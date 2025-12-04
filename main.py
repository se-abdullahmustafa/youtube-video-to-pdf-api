import os
import re
import cv2
import yt_dlp
import uuid
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from fpdf import FPDF
from PIL import Image

# Environment configuration
ENVIRONMENT = os.getenv('ENVIRONMENT', 'local').lower()

# Environment-specific settings
ENV_CONFIG = {
    'local': {
        'host': '0.0.0.0',
        'port': int(os.getenv('PORT', 8000)),
        'cors_origins': [
            'http://localhost:3001',
            'http://localhost:8080',
            'http://127.0.0.1:3001',
        ],
        'debug': os.getenv('DEBUG', 'true').lower() == 'true'
    },
    'production': {
        'host': '0.0.0.0',
        'port': int(os.getenv('PORT', 80)),
        'cors_origins': [
            'https://ytglancer.com',
        ],
        'debug': os.getenv('DEBUG', 'false').lower() == 'true'
    }
}

# Get current environment config
config = ENV_CONFIG.get(ENVIRONMENT, ENV_CONFIG['local'])

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
from starlette.responses import StreamingResponse as SSEStreamingResponse
from starlette.datastructures import Headers
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

# Create temp directory if it doesn't exist
TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

# Logs directory
LOGS_DIR = "logs"

# Configure logging
def setup_logging():
    # Create logs directory if it doesn't exist
    os.makedirs(LOGS_DIR, exist_ok=True)
    
    # Clear any existing handlers
    logging.getLogger().handlers.clear()
    
    # Set up formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Set up console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # Set up file handler with rotation (10 MB per file, keep 5 backup files)
    log_file = os.path.join(LOGS_DIR, 'app.log')
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10 MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    
    # Configure specific loggers
    logging.getLogger('uvicorn').setLevel(logging.WARNING)
    logging.getLogger('uvicorn.error').setLevel(logging.WARNING)
    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)
    
    return logging.getLogger(__name__)

# Initialize logger
logger = setup_logging()

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
            "name": "video-conversion",
            "description": "YouTube video to PDF conversion operations"
        }
    ]
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=config['cors_origins'],
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

# Progress tracking system
progress_store: Dict[str, Dict] = {}

class ProgressManager:
    @classmethod
    def create_task(cls, task_id: str):
        progress_store[task_id] = {
            "step": "starting",
            "progress": 0,
            "message": "Initializing conversion...",
            "timestamp": datetime.utcnow().isoformat()
        }
    
    @classmethod
    def update_progress(cls, task_id: str, step: str, progress: int, message: str):
        if task_id in progress_store:
            progress_store[task_id].update({
                "step": step,
                "progress": progress,
                "message": message,
                "timestamp": datetime.utcnow().isoformat()
            })
    
    @classmethod
    def get_progress(cls, task_id: str) -> Optional[Dict]:
        return progress_store.get(task_id)
    
    @classmethod
    def complete_task(cls, task_id: str, pdf_url: str = None):
        if task_id in progress_store:
            progress_store[task_id].update({
                "step": "completed",
                "progress": 100,
                "message": "Conversion completed successfully!",
                "pdf_url": pdf_url,
                "timestamp": datetime.utcnow().isoformat()
            })
    
    @classmethod
    def error_task(cls, task_id: str, error_message: str):
        if task_id in progress_store:
            progress_store[task_id].update({
                "step": "error",
                "progress": 0,
                "message": f"Error: {error_message}",
                "timestamp": datetime.utcnow().isoformat()
            })

@app.get("/progress/{task_id}")
async def get_progress(task_id: str):
    """Get current progress of a conversion task"""
    progress = ProgressManager.get_progress(task_id)
    if not progress:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(content=progress)

@app.get("/progress-stream/{task_id}")
async def progress_stream(task_id: str):
    """Server-Sent Events stream for real-time progress updates"""
    async def event_generator():
        # Check if task exists
        if task_id not in progress_store:
            yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
            return
        
        while True:
            progress = ProgressManager.get_progress(task_id)
            if progress:
                yield f"data: {json.dumps(progress)}\n\n"
                
                # Stop streaming if task is completed or errored
                if progress.get("step") in ["completed", "error"]:
                    break
            
            await asyncio.sleep(1)  # Send updates every second
    
    return SSEStreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        }
    )

@app.get("/download/{pdf_filename}")
async def download_pdf(pdf_filename: str):
    """Download the generated PDF"""
    # Look for PDF in temp folder first, then in root
    temp_path = os.path.join(TEMP_DIR, pdf_filename)
    root_path = os.path.join(os.getcwd(), pdf_filename)
    
    pdf_path = temp_path if os.path.exists(temp_path) else root_path
    
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="PDF not found")
    
    # Create a safe filename for the header
    safe_filename = pdf_filename.encode('ascii', errors='ignore').decode('ascii')
    if not safe_filename or len(safe_filename) < 3:
        safe_filename = "video.pdf"
    
    # Use only the safe filename in the header
    content_disposition = f"attachment; filename=\"{safe_filename}\""
    
    return FileResponse(
        pdf_path,
        media_type='application/pdf',
        filename=safe_filename,
        headers={"Content-Disposition": content_disposition}
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
    # Save PDF inside the output folder with YouTube video title as filename
    sanitized_title = sanitize_filename(video_title)
    pdf_file_name = os.path.join(output_folder, f'{sanitized_title}.pdf')
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
            'format': 'best[height<=480][ext=mp4]/best[height<=480]/best[ext=mp4]/best',  # Download 480p or lower
            'outtmpl': os.path.join(video_folder, 'video.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
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
    response_description="Returns task ID for progress tracking",
    responses={
        200: {
            "model": SuccessResponse,
            "description": "Returns task ID for tracking conversion progress"
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
    summary="Convert YouTube Video to PDF",
    description="Converts a YouTube video to a PDF by extracting frames at specified intervals with real-time progress updates",
    tags=["video-conversion"]
)
async def convert_video_to_pdf(
    request: Request,
    youtube_url: str = Query(..., description="YouTube video URL"),
    time_interval: int = Query(..., gt=0, le=3600, description="Time interval in seconds between frames"),
):
    """
    Convert a YouTube video to a PDF by extracting frames at specified intervals.
    
    - **youtube_url**: URL of the YouTube video to convert
    - **time_interval**: Time interval in seconds between frames (1-3600 seconds)
    
    Returns a task ID for tracking conversion progress.
    """
    logger.info(f"Starting video to PDF conversion for URL: {youtube_url}")
    
    # Validate time_interval parameter
    if time_interval <= 0 or time_interval > 3600:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Time interval must be between 1 and 3600 seconds (1 hour max)"
        )
    
    # Create task ID for progress tracking
    task_id = str(uuid.uuid4())
    ProgressManager.create_task(task_id)
    
    # Start conversion in background
    asyncio.create_task(background_conversion(task_id, youtube_url, time_interval))
    
    return JSONResponse(
        content={
            "status": "started",
            "message": "Conversion started successfully",
            "task_id": task_id,
            "progress_url": f"/progress/{task_id}",
            "progress_stream_url": f"/progress-stream/{task_id}"
        }
    )

async def background_conversion(task_id: str, youtube_url: str, time_interval: int):
    """Background task for video conversion with progress updates"""
    video_folder = None
    
    try:
        # Step 1: Download
        ProgressManager.update_progress(task_id, "downloading", 10, "Downloading YouTube video...")
        
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
        video_id = task_id[:8]  # Use first 8 chars of task ID
        video_folder = os.path.join(TEMP_DIR, f'temp_video_{video_id}')
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
            ProgressManager.update_progress(task_id, "downloading", 30, "Downloading video...")
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

        # Step 2: Frame Extraction
        ProgressManager.update_progress(task_id, "extracting", 50, "Extracting frames from video...")
        try:
            logger.info("Extracting frames...")
            await extract_frames_async(video_path, video_folder, time_interval)
            frame_count = len([f for f in os.listdir(video_folder) if f.startswith('frame_')])
            logger.info(f"Extracted {frame_count} frames")
            
            if frame_count == 0:
                raise FrameExtractionError(
                    "No frames were extracted from the video. The video might be too short or corrupted.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )
            
            # Delete the video file after successful frame extraction
            if os.path.exists(video_path):
                os.remove(video_path)
                logger.info(f"Deleted video file: {video_path}")
                
        except Exception as e:
            # Clean up video file if it exists
            if 'video_path' in locals() and video_path and os.path.exists(video_path):
                os.remove(video_path)
                logger.info(f"Cleaned up video file after error: {video_path}")
            raise FrameExtractionError(
                f"Error extracting frames: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Step 3: PDF Generation
        ProgressManager.update_progress(task_id, "building_pdf", 80, "Building PDF document...")
        try:
            logger.info("Creating PDF...")
            pdf_file = await create_pdf_async(video_folder, video_title)
            if not os.path.exists(pdf_file):
                raise PDFGenerationError(
                    "Failed to generate PDF",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            logger.info(f"PDF created successfully: {pdf_file}")
            
            # Delete all frame files after PDF is created
            frame_files = [f for f in os.listdir(video_folder) if f.startswith('frame_')]
            for frame_file in frame_files:
                frame_path = os.path.join(video_folder, frame_file)
                if os.path.exists(frame_path):
                    os.remove(frame_path)
            logger.info(f"Deleted {len(frame_files)} frame files")
        except Exception as e:
            raise PDFGenerationError(
                f"Error generating PDF: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Step 4: Done
        ProgressManager.update_progress(task_id, "done", 95, "Finalizing...")
        
        # Move PDF to temp folder
        pdf_filename = os.path.basename(pdf_file)
        final_pdf_path = os.path.join(TEMP_DIR, pdf_filename)
        shutil.move(pdf_file, final_pdf_path)
        
        # Clean up temporary folder
        await cleanup_folder_async(video_folder)
        
        # Complete task
        ProgressManager.complete_task(task_id, f"/download/{pdf_filename}")
        logger.info(f"Conversion completed successfully for task {task_id}")

    except HTTPException:
        ProgressManager.error_task(task_id, "HTTP error occurred")
        raise
    except VideoProcessingError as e:
        logger.error(f"Video processing error: {str(e)}")
        ProgressManager.error_task(task_id, str(e))
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        ProgressManager.error_task(task_id, "An unexpected error occurred")
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

@app.get("/download/{pdf_filename}")
async def download_pdf(pdf_filename: str):
    """Download the generated PDF"""
    # Look for PDF in temp folder first, then in root
    temp_path = os.path.join(TEMP_DIR, pdf_filename)
    root_path = os.path.join(os.getcwd(), pdf_filename)
    
    pdf_path = temp_path if os.path.exists(temp_path) else root_path
    
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="PDF not found")
    
    # Create a safe filename for the header
    safe_filename = pdf_filename.encode('ascii', errors='ignore').decode('ascii')
    if not safe_filename or len(safe_filename) < 3:
        safe_filename = "video.pdf"
    
    # Use only the safe filename in the header
    content_disposition = f"attachment; filename=\"{safe_filename}\""
    
    return FileResponse(
        pdf_path,
        media_type='application/pdf',
        filename=safe_filename,
        headers={"Content-Disposition": content_disposition}
    )

if __name__ == '__main__':
    import uvicorn
    print(f"Starting server in {ENVIRONMENT} environment...")
    print(f"Server will be available at http://{config['host']}:{config['port']}")
    print(f"CORS origins: {config['cors_origins']}")
    
    uvicorn.run(
        app, 
        host=config['host'], 
        port=config['port'],
        reload=config['debug']
    )
