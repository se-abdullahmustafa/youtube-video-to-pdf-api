import os
import re
import cv2
import yt_dlp
import uuid
import asyncio
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import (
    FastAPI, 
    Query, 
    HTTPException, 
    status, 
    Request, 
    Depends
)
from fastapi.responses import (
    FileResponse, 
    JSONResponse, 
    Response,
    StreamingResponse
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, HttpUrl, Field, validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from concurrent.futures import ThreadPoolExecutor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log')
    ]
)
logger = logging.getLogger(__name__)

# Thread pool for CPU-bound operations
executor = ThreadPoolExecutor(max_workers=4)

# Request and Response Models
class VideoConversionRequest(BaseModel):
    """Request model for video conversion"""
    youtube_url: HttpUrl = Field(..., description="URL of the YouTube video to convert")
    time_interval: int = Field(
        ..., 
        gt=0, 
        le=60,
        description="Time interval in minutes between frames (1-60 minutes)"
    )

    @validator('youtube_url')
    def validate_youtube_url(cls, v):
        """Validate that the URL is a valid YouTube URL"""
        youtube_regex = (
            r'(https?://)?(www\.)?'
            '(youtube|youtu|youtube-nocookie)\.(com|be)/'
            '(watch\?v=|embed/|v/|.+/|\?v=|&v=|\/v\/)?([^&=%\?\/"]{11})'
        )
        if not re.match(youtube_regex, str(v)):
            raise ValueError("Invalid YouTube URL")
        return v

class ErrorResponse(BaseModel):
    """Standard error response model"""
    status_code: int
    error: str
    message: str
    timestamp: str
    path: str
    request_id: str

class SuccessResponse(BaseModel):
    """Standard success response model"""
    status: str = "success"
    message: str
    data: Optional[Dict[str, Any]] = None
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

# Custom exception classes
class VideoProcessingError(Exception):
    """Custom exception for video processing errors"""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)

class VideoDownloadError(VideoProcessingError):
    """Exception raised for errors in video downloading"""
    pass

class FrameExtractionError(VideoProcessingError):
    """Exception raised for errors in frame extraction"""
    pass

class PDFGenerationError(VideoProcessingError):
    """Exception raised for errors in PDF generation"""
    pass

# Initialize FastAPI with metadata
app = FastAPI(
    title="YouTube Video to PDF Converter API",
    description="A high-performance API for converting YouTube videos to PDF documents",
    version="1.0.0",
    contact={
        "name": "API Support",
        "email": "support@example.com"
    },
    license_info={
        "name": "MIT",
    },
    openapi_tags=[
        {
            "name": "conversion",
            "description": "Video to PDF conversion operations"
        }
    ]
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://yourdomain.com",
        "http://207.180.210.137",
        "http://localhost:8080",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"]
)

# Add request logging middleware
class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        
        # Log request
        logger.info(
            f"Request: {request.method} {request.url} | "
            f"Headers: {dict(request.headers)} | "
            f"Query Params: {dict(request.query_params)}"
        )
        
        try:
            response = await call_next(request)
            logger.info(
                f"Response: {request.method} {request.url} | "
                f"Status: {response.status_code} | "
                f"Request ID: {request_id}"
            )
            return response
        except Exception as e:
            logger.error(
                f"Error: {request.method} {request.url} | "
                f"Error: {str(e)} | "
                f"Request ID: {request_id}",
                exc_info=True
            )
            raise

app.add_middleware(LoggingMiddleware)

# Global exception handlers
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    error_response = ErrorResponse(
        status_code=exc.status_code,
        error=exc.detail if hasattr(exc, 'detail') else "HTTP Exception",
        message=str(exc.detail) if hasattr(exc, 'detail') else str(exc),
        timestamp=datetime.utcnow().isoformat(),
        path=request.url.path,
        request_id=request.state.request_id if hasattr(request.state, 'request_id') else ""
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(error_response)
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_response = ErrorResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        error="Validation Error",
        message="Invalid request data",
        timestamp=datetime.utcnow().isoformat(),
        path=request.url.path,
        request_id=request.state.request_id if hasattr(request.state, 'request_id') else ""
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder(error_response)
    )

@app.exception_handler(VideoProcessingError)
async def video_processing_exception_handler(request: Request, exc: VideoProcessingError):
    error_response = ErrorResponse(
        status_code=exc.status_code,
        error=exc.__class__.__name__,
        message=str(exc),
        timestamp=datetime.utcnow().isoformat(),
        path=request.url.path,
        request_id=request.state.request_id if hasattr(request.state, 'request_id') else ""
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(error_response)
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {str(exc)}", exc_info=True)
    error_response = ErrorResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error="Internal Server Error",
        message="An unexpected error occurred",
        timestamp=datetime.utcnow().isoformat(),
        path=request.url.path,
        request_id=request.state.request_id if hasattr(request.state, 'request_id') else ""
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=jsonable_encoder(error_response)
    )

def sanitize_filename(file_name: str) -> str:
    """
    Sanitize a string to be used as a filename.
    
    Args:
        file_name: The original filename to sanitize
        
    Returns:
        str: Sanitized filename with invalid characters replaced by underscores
    """
    if not file_name or not isinstance(file_name, str):
        return "unnamed_video"
        
    # Remove invalid characters
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F\x7F]', '_', file_name)
    
    # Limit length and remove trailing spaces/dots
    sanitized = sanitized.strip('. ')[:100].strip()
    
    # If after sanitization the name is empty, return a default name
    if not sanitized:
        return "unnamed_video"
        
    return sanitized

def extract_frames(video_path, output_folder, seconds):
    video_capture = cv2.VideoCapture(video_path)
    frame_rate = int(video_capture.get(cv2.CAP_PROP_FPS))
    print("frame rate:", frame_rate)
    total_frames = int(video_capture.get(cv2.CAP_PROP_FRAME_COUNT))
    print("total frame:", total_frames)
    # Calculate frame interval based on seconds and frame rate
    frame_interval = int(frame_rate * int(seconds))
    print("seconds", seconds)
    print("frame interval:", (frame_interval))
    # Make sure frame_interval is not zero to avoid division by zero
    if frame_interval == 0:
        frame_interval = 1
    for i in range(0, total_frames, frame_interval):
        video_capture.set(cv2.CAP_PROP_POS_FRAMES, i)
        success, image = video_capture.read()
        if success:
            frame_path = os.path.join(output_folder, f'frame_{i}.jpg')
            cv2.imwrite(frame_path, image)
    video_capture.release()

def create_pdf_from_frames(output_folder, video_title="video"):
    pdf = FPDF(format='A4')  # Adjust format as needed
    for root, _, files in os.walk(output_folder):
        image_files = [file for file in files if file.endswith('.jpg')]
        image_files.sort()
        for image_file in image_files:
            image_path = os.path.join(root, image_file)
            with Image.open(image_path) as img:
                img_width, img_height = img.size
            # Calculate scaled dimensions to fit within PDF page
            pdf_width, pdf_height = pdf.w, pdf.h
            scale = min(pdf_width / img_width, pdf_height / img_height)
            new_width = img_width * scale
            new_height = img_height * scale
            # Calculate center coordinates
            center_x = (pdf_width - new_width) / 2
            center_y = (pdf_height - new_height) / 2
            pdf.add_page()
            pdf.image(image_path, x=center_x, y=center_y, w=new_width, h=new_height)
    # Use video title for PDF filename
    sanitized_title = sanitize_filename(video_title)
    pdf_file_name = f'{sanitized_title}.pdf'
    pdf.output(pdf_file_name)
    return pdf_file_name

async def download_video_async(youtube_url: str, video_folder: str) -> str:
    """
    Download a YouTube video using yt-dlp.
    
    Args:
        youtube_url: URL of the YouTube video
        video_folder: Directory to save the downloaded video
        
    Returns:
        str: Path to the downloaded video file
        
    Raises:
        VideoDownloadError: If there's an error downloading the video
    """
    try:
        ydl_opts = {
            'format': 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': os.path.join(video_folder, 'video.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'ignoreerrors': False,
            'nooverwrites': True,
            'retries': 3,
            'fragment_retries': 10,
            'extractor_retries': 3,
            'buffer_size': 1024 * 1024,  # 1MB buffer
            'http_chunk_size': 1048576,  # 1MB chunks
            'extract_flat': False,
            'restrictfilenames': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Get video info first to validate
            info_dict = ydl.extract_info(youtube_url, download=False)
            
            # Check if video is available
            if not info_dict or 'url' not in info_dict:
                raise VideoDownloadError(
                    "Could not retrieve video information. The video might be private or unavailable.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )
                
            # Check for age restriction
            if info_dict.get('age_limit', 0) > 18:
                raise VideoDownloadError(
                    "Age-restricted content. Please sign in to YouTube to verify your age.",
                    status_code=status.HTTP_403_FORBIDDEN
                )
                
            # Download the video
            info_dict = ydl.extract_info(youtube_url, download=True)
            video_path = ydl.prepare_filename(info_dict)
            
            # Verify the file was downloaded
            if not os.path.exists(video_path):
                raise VideoDownloadError(
                    "Failed to save the video file.",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
            # Verify file size is reasonable (min 100KB, max 2GB)
            file_size = os.path.getsize(video_path)
            if file_size < 102400:  # 100KB
                os.remove(video_path)
                raise VideoDownloadError(
                    "The downloaded file is too small and might be corrupted.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )
                
            if file_size > 2 * 1024 * 1024 * 1024:  # 2GB
                os.remove(video_path)
                raise VideoDownloadError(
                    "The video is too large to process. Maximum size is 2GB.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )
            
            logger.info(f"Downloaded video: {video_path} (Size: {file_size/1024/1024:.2f}MB)")
            return video_path
            
    except yt_dlp.DownloadError as e:
        raise VideoDownloadError(
            f"Failed to download video: {str(e)}",
            status_code=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        logger.error(f"Error in download_video_async: {str(e)}", exc_info=True)
        raise VideoDownloadError(
            f"An error occurred while downloading the video: {str(e)}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

async def extract_frames_async(video_path, output_folder, seconds):
    loop = asyncio.get_event_loop()
    # Run CPU-intensive frame extraction in thread pool
    await loop.run_in_executor(
        executor,
        extract_frames,
        video_path,
        output_folder,
        seconds
    )

async def create_pdf_async(output_folder, video_title="video"):
    loop = asyncio.get_event_loop()
    # Run PDF creation in thread pool
    pdf_file = await loop.run_in_executor(
        executor,
        create_pdf_from_frames,
        output_folder,
        video_title
    )
    return pdf_file

async def cleanup_folder_async(folder_path):
    loop = asyncio.get_event_loop()
    # Run file cleanup in thread pool
    await loop.run_in_executor(
        executor,
        shutil.rmtree,
        folder_path
    )

@app.get(
    "/convert_video_to_pdf",
    response_description="Returns the generated PDF file",
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Returns the generated PDF file",
        },
        400: {
            "model": ErrorResponse,
            "description": "Invalid request parameters or video URL"
        },
        422: {
            "model": ErrorResponse,
            "description": "Validation error"
        },
        500: {
            "model": ErrorResponse,
            "description": "Internal server error"
        }
    },
    summary="Convert YouTube video to PDF",
    description="Converts a YouTube video to a PDF by extracting frames at specified intervals",
    tags=["conversion"]
)
async def convert_video_to_pdf(
    request: Request,
    youtube_url: str = Query(..., description="URL of the YouTube video to convert"),
    time: int = Query(None, gt=0, le=3600, description="Time interval in seconds between frames (1-3600 seconds)"),
    time_interval: int = Query(None, gt=0, le=3600, description="Time interval in seconds between frames (1-3600 seconds)"),
):
    """
    Convert a YouTube video to a PDF by extracting frames at specified intervals.
    
    - **youtube_url**: URL of the YouTube video to convert
    - **time_interval**: Time interval in seconds between frames (1-3600 seconds)
    
    Returns the generated PDF file for download.
    """
    logger.info(f"Starting video to PDF conversion for URL: {youtube_url}")
    
    # Handle both time and time_interval parameters for backward compatibility
    if time_interval is None and time is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'time' or 'time_interval' parameter is required"
        )
    
    # Use time_interval if provided, otherwise use time
    final_time_interval = time_interval if time_interval is not None else time
    
    if final_time_interval <= 0 or final_time_interval > 3600:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Time interval must be between 1 and 3600 seconds (1 hour max)"
        )
    
    video_folder = None
    
    try:
        # Validate YouTube URL
        youtube_regex = (
            r'(https?://)?(www\.)?'
            '(youtube|youtu|youtube-nocookie)\.(com|be)/'
            '(watch\?v=|embed/|v/|.+/|\?v=|&v=|\/v\/)?([^&=%\?\/"]{11})'
        )
        if not re.match(youtube_regex, youtube_url):
            raise VideoProcessingError(
                "Invalid YouTube URL. Please provide a valid YouTube video URL.",
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Create a unique folder for this conversion
        video_id = str(uuid.uuid4())
        video_folder = f'temp_video_{video_id}'
        os.makedirs(video_folder, exist_ok=True)
        logger.info(f"Created temporary directory: {video_folder}")

        # Get video info first
        try:
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
                info_dict = ydl.extract_info(youtube_url, download=False)
                video_title = info_dict.get('title', 'video')
                video_duration = info_dict.get('duration', 0)
                
                # Validate video duration (max 2 hours)
                if video_duration > 7200:  # 2 hours in seconds
                    raise VideoProcessingError(
                        "Video is too long. Maximum allowed duration is 2 hours.",
                        status_code=status.HTTP_400_BAD_REQUEST
                    )
                
                logger.info(f"Video info - Title: {video_title}, Duration: {video_duration}s")
        except Exception as e:
            raise VideoDownloadError(
                f"Failed to get video info: {str(e)}",
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Download the video using yt-dlp
        try:
            logger.info("Starting video download...")
            video_path = await download_video_async(youtube_url, video_folder)
            if not video_path or not os.path.exists(video_path):
                raise VideoDownloadError(
                    "Failed to download the video. Please check the URL and try again.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )
            logger.info(f"Video downloaded successfully: {video_path}")
        except Exception as e:
            raise VideoDownloadError(
                f"Error downloading video: {str(e)}",
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Extract frames
        try:
            logger.info("Extracting frames...")
            await extract_frames_async(video_path, video_folder, final_time_interval)
            frame_count = len([f for f in os.listdir(video_folder) if f.startswith('frame_')])
            logger.info(f"Extracted {frame_count} frames")
            
            if frame_count == 0:
                raise FrameExtractionError(
                    "No frames were extracted from the video. The video might be too short or corrupted.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )
        except Exception as e:
            raise FrameExtractionError(
                f"Error extracting frames: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Create PDF
        try:
            logger.info("Creating PDF...")
            pdf_file = await create_pdf_async(video_folder, video_title)
            if not os.path.exists(pdf_file):
                raise PDFGenerationError(
                    "Failed to generate PDF",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            logger.info(f"PDF created successfully: {pdf_file}")
        except Exception as e:
            raise PDFGenerationError(
                f"Error generating PDF: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Prepare response
        response_headers = {
            "Content-Disposition": f"attachment; filename=\"{sanitize_filename(video_title)}.pdf\"",
            "X-Request-ID": request.state.request_id if hasattr(request.state, 'request_id') else "",
            "X-Video-Title": video_title,
            "X-Frame-Count": str(frame_count),
            "X-Video-Duration": str(video_duration)
        }

        # Return the PDF file using StreamingResponse for large files
        def file_stream():
            with open(pdf_file, "rb") as f:
                while chunk := f.read(65536):  # 64KB chunks
                    yield chunk
            
            # Clean up after streaming is complete
            if os.path.exists(pdf_file):
                os.remove(pdf_file)

        return StreamingResponse(
            file_stream(),
            media_type='application/pdf',
            headers=response_headers
        )

    except HTTPException:
        raise
    except VideoProcessingError as e:
        logger.error(f"Video processing error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing your request."
        )
    finally:
        # Ensure cleanup happens even if there's an error
        if video_folder and os.path.exists(video_folder):
            try:
                await cleanup_folder_async(video_folder)
                logger.info(f"Cleaned up directory: {video_folder}")
            except Exception as e:
                logger.error(f"Error during cleanup: {str(e)}")

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
