import os, re, cv2, yt_dlp, uuid, asyncio, logging, json, shutil, time, threading
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor
from fpdf import FPDF
from dotenv import load_dotenv
from fastapi import FastAPI, Query, HTTPException, status, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

# Load environment variables from .env file
load_dotenv()

# ==================== CONFIG ====================
ENVIRONMENT = os.getenv('ENVIRONMENT', 'local').lower()

# Read configuration from .env file with defaults
PORT = int(os.getenv('PORT', '8000'))
HOST = os.getenv('HOST', '0.0.0.0')
DEBUG = os.getenv('DEBUG', 'true').lower() == 'true'
MAX_WORKERS = int(os.getenv('MAX_WORKERS', '16'))
MAX_TASKS = int(os.getenv('MAX_TASKS', '500'))
CLEANUP_HOURS = int(os.getenv('CLEANUP_HOURS', '24'))

# Environment-specific defaults (can be overridden by .env)
ENV_CONFIG = {
    'local': {'host': HOST, 'port': PORT, 'debug': DEBUG, 'max_workers': MAX_WORKERS, 'max_tasks': MAX_TASKS, 'cleanup_hours': CLEANUP_HOURS},
    'production': {
        'host': HOST, 
        'port': int(os.getenv('PORT', '80')),  # Default 80 for production if not in .env
        'debug': False, 
        'max_workers': int(os.getenv('MAX_WORKERS', '128')), 
        'max_tasks': int(os.getenv('MAX_TASKS', '5000')), 
        'cleanup_hours': int(os.getenv('CLEANUP_HOURS', '12'))
    }
}
config = ENV_CONFIG.get(ENVIRONMENT, ENV_CONFIG['local'])

# Proxy configuration from .env file for both environments
PROXY_URL = os.getenv('PROXY_URL', '')

# Additional optional .env settings
TEMP_DIR = os.getenv('TEMP_DIR', 'temp')
LOGS_DIR = os.getenv('LOGS_DIR', 'logs')
CLEANUP_INTERVAL = int(os.getenv('CLEANUP_INTERVAL', '3600'))  # 1 hour in seconds (default)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# ==================== LOGGING ====================
class JSONLFormatter(logging.Formatter):
    """Custom formatter that outputs logs in JSONL (JSON Lines) format"""
    def format(self, record):
        log_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': record.levelname,
            'message': record.getMessage(),
            'logger': record.name
        }
        return json.dumps(log_data)

def setup_logging():
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_h = RotatingFileHandler(f'{LOGS_DIR}/app.log', maxBytes=10*1024*1024, backupCount=5)
    file_h.setFormatter(formatter)
    
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_h)
    
    for name in ['uvicorn', 'uvicorn.error', 'uvicorn.access', 'watchfiles']:
        logging.getLogger(name).setLevel(logging.WARNING)
    return logging.getLogger(__name__)

def get_conversion_logger():
    """Get the shared conversion logger for all tasks"""
    conversion_logger = logging.getLogger('conversions')
    if conversion_logger.handlers:
        return conversion_logger
    
    conversion_logger.setLevel(logging.INFO)
    conversion_logger.propagate = False  # avoid double logging to root handlers
    
    # Create JSONL formatter for conversion logs
    jsonl_formatter = JSONLFormatter()
    
    # File handler for unified conversion log in JSONL format
    log_file = os.path.join(LOGS_DIR, 'conversions.log')
    file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setFormatter(jsonl_formatter)
    conversion_logger.addHandler(file_handler)
    
    return conversion_logger

logger = setup_logging()
executor = ThreadPoolExecutor(max_workers=config['max_workers'])

# Track PDF downloads
pdf_downloads: Dict[str, bool] = {}

# ==================== LIFECYCLE ====================
from contextlib import asynccontextmanager

async def cleanup_task():
    """Periodically clean up old tasks and temporary files"""
    conv_logger = get_conversion_logger()
    
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            TaskManager.cleanup_old_tasks()
            
            # Clean up temp files for deleted tasks and update logs with download status
            if os.path.exists(TEMP_DIR):
                pdf_files = [f for f in os.listdir(TEMP_DIR) if f.endswith('.pdf')]
                for pdf_file in pdf_files:
                    # Check if task exists for this PDF
                    task_found = False
                    for task in TaskManager.get_all_tasks():
                        if task.get('pdf_filename') == pdf_file:
                            task_found = True
                            break
                    
                    if not task_found:
                        try:
                            pdf_path = os.path.join(TEMP_DIR, pdf_file)
                            if os.path.exists(pdf_path):
                                os.remove(pdf_path)
                                logger.info(f"Cleaned up orphaned PDF: {pdf_file}")
                                # Mark as not downloaded in tracking
                                pdf_downloads[pdf_file] = False
                        except Exception as e:
                            logger.warning(f"Failed to clean up {pdf_file}: {e}")
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info(f"Starting server in {ENVIRONMENT} mode with max {config['max_tasks']} concurrent tasks")
    cleanup_task_handle = asyncio.create_task(cleanup_task())
    
    yield  # This is where the application runs
    
    # Shutdown
    cleanup_task_handle.cancel()
    try:
        await cleanup_task_handle
    except asyncio.CancelledError:
        pass

# ==================== APP SETUP ====================
app = FastAPI(
    title="YouTube to PDF API",
    version="2.0.0",
    description="Fast & reliable YouTube to PDF converter",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
    lifespan=lifespan
)

app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
app.add_middleware(GZipMiddleware, minimum_size=1000)

class LogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())[:8]
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        logger.info(f"{request.method} {request.url.path} - {response.status_code} - {duration:.2f}s")
        return response

app.add_middleware(LogMiddleware)

# ==================== EXCEPTION HANDLERS ====================
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": "HTTP Error", "message": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": "Validation Error", "message": "Invalid parameters"})

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Error: {str(exc)}", exc_info=True)
    return JSONResponse(status_code=500, content={"error": "Internal Server Error", "message": str(exc)[:100]})

# ==================== TASK MANAGEMENT ====================
task_store: Dict[str, Dict] = {}
active_tasks: set = set()
task_lock = threading.RLock()

class TaskManager:
    @classmethod
    def create_task(cls, task_id: str):
        with task_lock:
            if len(active_tasks) >= config['max_tasks']:
                raise HTTPException(status_code=429, detail="Server at maximum capacity")
            task_store[task_id] = {
                "task_id": task_id,
                "status": "processing",
                "progress": 0,
                "message": "Starting...",
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }
            active_tasks.add(task_id)
    
    @classmethod
    def update_task(cls, task_id: str, progress: int, message: str, total_size_mb: float = None, downloaded_size_mb: float = None, download_percentage: float = None, download_threads: int = None):
        with task_lock:
            if task_id in task_store:
                update_data = {"progress": progress, "message": message, "updated_at": datetime.utcnow().isoformat()}
                if total_size_mb is not None:
                    update_data["total_size_mb"] = round(total_size_mb, 2)
                if downloaded_size_mb is not None:
                    update_data["downloaded_size_mb"] = round(downloaded_size_mb, 2)
                if download_percentage is not None:
                    update_data["download_percentage"] = round(download_percentage, 1)
                if download_threads is not None:
                    update_data["download_threads"] = download_threads
                task_store[task_id].update(update_data)
    
    @classmethod
    def complete_task(cls, task_id: str, pdf_filename: str):
        with task_lock:
            if task_id in task_store:
                task_store[task_id].update({
                    "status": "completed",
                    "progress": 100,
                    "message": "Done",
                    "pdf_filename": pdf_filename,
                    "completed_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat()
                })
                active_tasks.discard(task_id)
    
    @classmethod
    def error_task(cls, task_id: str, error: str):
        with task_lock:
            if task_id in task_store:
                task_store[task_id].update({
                    "status": "error",
                    "progress": 0,
                    "message": f"Error: {error[:50]}",
                    "completed_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat()
                })
                active_tasks.discard(task_id)
    
    @classmethod
    def get_task(cls, task_id: str):
        with task_lock:
            return task_store.get(task_id)
    
    @classmethod
    def get_all_tasks(cls, status: str = None):
        with task_lock:
            tasks = list(task_store.values())
            return [t for t in tasks if t.get('status') == status] if status else tasks
    
    @classmethod
    def cleanup_old_tasks(cls):
        """Remove completed/error tasks older than cleanup_hours"""
        with task_lock:
            cutoff_time = datetime.utcnow() - timedelta(hours=config['cleanup_hours'])
            to_delete = []
            for task_id, task in task_store.items():
                if task.get('status') in ['completed', 'error']:
                    completed_at = task.get('completed_at')
                    if completed_at:
                        try:
                            task_time = datetime.fromisoformat(completed_at)
                            if task_time < cutoff_time:
                                to_delete.append(task_id)
                        except:
                            pass
            
            for task_id in to_delete:
                del task_store[task_id]
                logger.info(f"Cleaned up old task: {task_id}")

# ==================== VIDEO PROCESSING ====================
def download_video(youtube_url: str, output_path: str, task_id: str = None, progress_callback=None) -> tuple:
    """
    Download YouTube video with progress tracking and optimized format selection.
    Uses yt-dlp internal fragment threading plus our executor to stay non-blocking for FastAPI.
    Returns: (title, quality, size_mb)
    """
    try:
        # Optimized format selector: video-only at max 480p for faster downloads
        # We only need video frames, so no audio needed - this significantly speeds up download
        # STRICT: Only download up to 480p maximum resolution
        # Exclude AV1 (av01) because FFmpeg/OpenCV often fail to decode it on headless servers
        # Prefer DASH formats for parallel fragment downloading
        format_selector = (
            'bestvideo[height<=480][vcodec!*=av01][ext=mp4][protocol*=dash]/'  # DASH MP4 480p, no AV1
            'bestvideo[height<=480][vcodec!*=av01][ext=mp4]/'                  # MP4 480p, no AV1
            'bestvideo[height<=480][vcodec*=h264][protocol*=dash]/'            # H.264 DASH 480p
            'bestvideo[height<=480][vcodec*=h264]/'                            # H.264 480p
            'bestvideo[height<=480][vcodec*=vp9]/'                             # VP9 480p
            'bestvideo[height<=480][vcodec!*=av01][protocol*=dash]/'           # DASH 480p any, no AV1
            'bestvideo[height<=480][vcodec!*=av01]/'                           # Any 480p, no AV1
            'worstvideo[height<=480][vcodec!*=av01]'                           # Worst 480p, no AV1
        )

        # Bound per-download fragment threads to stay within overall executor capacity
        fragment_threads = max(4, min(16, config['max_workers']))

        opts = {
            'format': format_selector,
            'outtmpl': output_path,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 60,
            'http_chunk_size': 10485760,  # 10MB chunks for better performance
            'concurrent_fragment_downloads': fragment_threads,  # Threaded fragment downloads
            'retries': 10,  # More retries for reliability
            'fragment_retries': 10,
            'file_access_retries': 3,
            'noplaylist': True,  # Don't download playlists
            'noprogress': False,
            'extract_flat': False,
            # Enable parallel downloading optimizations
            'external_downloader': None,  # Use native downloader with threading
            'external_downloader_args': None,
            'prefer_insecure': False,
            'no_check_certificate': False,
            # Convert to H.264 MP4 if the downloaded stream is not natively supported
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                # yt_dlp only supports preferedformat here; codec is inferred
                'preferedformat': 'mp4',
            }],
        }
        # Only add proxy if configured
        if PROXY_URL:
            opts['proxy'] = PROXY_URL

        # Progress hook for real-time download updates with size tracking
        total_size_bytes = 0
        download_threads = opts.get('concurrent_fragment_downloads', None)

        def progress_hook(d):
            nonlocal total_size_bytes
            if d['status'] == 'downloading' and progress_callback and task_id:
                # Get total size (use estimate if exact not available)
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                if total > 0:
                    total_size_bytes = total
                
                downloaded = d.get('downloaded_bytes', 0)
                
                if total_size_bytes > 0:
                    # Calculate sizes in MB
                    total_size_mb = total_size_bytes / (1024 * 1024)
                    downloaded_size_mb = downloaded / (1024 * 1024)
                    download_percentage = (downloaded / total_size_bytes) * 100
                    
                    # Download progress is 0-15% of overall (15% because download is 0-15%, then frames 15-75%, PDF 75-100%)
                    overall_progress = min(15, int((downloaded / total_size_bytes) * 15))
                    
                    speed = d.get('speed', 0)
                    if speed:
                        speed_mb = speed / (1024 * 1024)
                        message = f"Downloading... {downloaded_size_mb:.1f}/{total_size_mb:.1f} MB ({download_percentage:.1f}%) - {speed_mb:.1f} MB/s"
                    else:
                        message = f"Downloading... {downloaded_size_mb:.1f}/{total_size_mb:.1f} MB ({download_percentage:.1f}%)"
                    
                    progress_callback(task_id, 5 + overall_progress, message, total_size_mb, downloaded_size_mb, download_percentage, download_threads)
                elif downloaded > 0:
                    downloaded_size_mb = downloaded / (1024 * 1024)
                    progress_callback(task_id, 10, f"Downloading... {downloaded_size_mb:.1f} MB", None, downloaded_size_mb, None, download_threads)
            elif d['status'] == 'finished':
                if progress_callback and task_id:
                    # Final update with actual file size
                    if os.path.exists(output_path):
                        file_size = os.path.getsize(output_path)
                        file_size_mb = file_size / (1024 * 1024)
                        progress_callback(task_id, 15, f"Download complete ({file_size_mb:.1f} MB), processing...", file_size_mb, file_size_mb, 100.0, download_threads)
                    else:
                        progress_callback(task_id, 15, "Download complete, processing...", None, None, None, download_threads)
        
        # Attach hook after creation to avoid missing early events
        opts['progress_hooks'] = [progress_hook]

        with yt_dlp.YoutubeDL(opts) as ydl:
            if progress_callback and task_id:
                progress_callback(task_id, 3, "Extracting video information...")
            info = ydl.extract_info(youtube_url, download=True)
            
            title = info.get('title', 'video')
            height = info.get('height') or info.get('format_note', '').replace('p', '')
            quality = f"{height}p" if height else "unknown"
            
            # Get file size
            file_size = 0
            if os.path.exists(output_path):
                file_size = os.path.getsize(output_path) / (1024 * 1024)
            
            return (title, quality, file_size)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Download failed: {error_msg}")
        # Clean up error message - remove ANSI codes and extract clean error
        if 'ERROR:' in error_msg:
            error_msg = error_msg.split('ERROR:')[-1].strip()
        # Remove ANSI color codes
        error_msg = re.sub(r'\x1b\[[0-9;]*m', '', error_msg)
        # Raise regular Exception (HTTPException won't work from thread executor)
        raise Exception(f"Failed to download: {error_msg[:200]}")

def extract_frames(video_path: str, output_folder: str, interval_seconds: int, task_id: str = None, progress_callback=None) -> int:
    """
    Extract frames from video with progress tracking.
    Progress range: 15-75% (frames extraction is 60% of total work)
    """
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise Exception(f"Cannot open video file (unsupported/invalid): {os.path.basename(video_path)}")
        
        fps = int(cap.get(cv2.CAP_PROP_FPS))
        if fps <= 0:
            cap.release()
            raise Exception("Invalid video: could not read FPS (possibly unsupported codec)")
        
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            raise Exception("Invalid video: no frames found (possibly unsupported codec)")
        frame_interval = max(1, fps * interval_seconds)
        estimated_frame_count = max(1, total_frames // frame_interval)
        
        if progress_callback and task_id:
            progress_callback(task_id, 18, f"Analyzing video ({total_frames} frames, {fps} fps)...")
        
        frame_count = 0
        frame_idx = 0
        last_update_time = time.time()
        
        while True:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            success, frame = cap.read()
            if not success:
                break
            
            # Add timestamp to top-right corner
            # Format: HH:MM:SS only if hour-long, MM:SS if less than hour, SS if less than minute
            timestamp_seconds = int(frame_idx / fps)
            hours = timestamp_seconds // 3600
            minutes = (timestamp_seconds % 3600) // 60
            seconds = timestamp_seconds % 60
            
            # Format timestamp: only include hours if > 0, only include minutes if > 0 or hours > 0
            if hours > 0:
                timestamp_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            elif minutes > 0:
                timestamp_str = f"{minutes:02d}:{seconds:02d}"
            else:
                timestamp_str = f"{seconds:02d}"
            
            # Add timestamp text to top-right corner
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.8
            thickness = 2
            color = (255, 255, 255)  # White text
            text_size = cv2.getTextSize(timestamp_str, font, font_scale, thickness)[0]
            x = frame.shape[1] - text_size[0] - 10  # 10px margin from right
            y = text_size[1] + 10  # 10px margin from top
            # Add black background for better readability
            cv2.rectangle(frame, (x - 5, y - text_size[1] - 5), (x + text_size[0] + 5, y + 5), (0, 0, 0), -1)
            cv2.putText(frame, timestamp_str, (x, y), font, font_scale, color, thickness)
            
            cv2.imwrite(os.path.join(output_folder, f'frame_{frame_count:04d}.jpg'), frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            frame_count += 1
            frame_idx += int(frame_interval)
            
            # Update progress every 0.5 seconds or every 10 frames
            current_time = time.time()
            if progress_callback and task_id and (current_time - last_update_time > 0.5 or frame_count % 10 == 0):
                # Progress from 20% to 70% during frame extraction
                if estimated_frame_count > 0:
                    frame_progress = min(60, int((frame_count / estimated_frame_count) * 60))
                    progress = 15 + frame_progress
                else:
                    progress = 20 + int((frame_idx / total_frames) * 50) if total_frames > 0 else 40
                progress_callback(task_id, progress, f"Extracting frames... {frame_count} frames ({timestamp_str})")
                last_update_time = current_time
        
        cap.release()
        if progress_callback and task_id:
            progress_callback(task_id, 75, f"Extracted {frame_count} frames, creating PDF...")
        return frame_count
    except Exception as e:
        logger.error(f"Frame extraction failed: {str(e)}")
        raise

def create_pdf(frames_folder: str, output_path: str, task_id: str = None, progress_callback=None):
    """
    Create PDF from frames with progress tracking.
    PDF dimensions match the frame dimensions exactly.
    Progress range: 75-100% (PDF creation is 25% of total work)
    """
    try:
        frames = sorted([f for f in os.listdir(frames_folder) if f.endswith('.jpg')])
        if not frames:
            raise Exception("No frames extracted")
        
        total_frames = len(frames)
        if progress_callback and task_id:
            progress_callback(task_id, 75, f"Creating PDF from {total_frames} frames...")
        
        # Get frame dimensions from first frame
        first_frame_path = os.path.join(frames_folder, frames[0])
        first_frame = cv2.imread(first_frame_path)
        if first_frame is None:
            raise Exception("Cannot read first frame")
        
        frame_height, frame_width = first_frame.shape[:2]
        # Convert pixels to mm (assuming 96 DPI: 1 inch = 25.4mm, 96 pixels = 25.4mm)
        # So 1 pixel = 25.4/96 mm ≈ 0.2646 mm
        width_mm = frame_width * 0.2646
        height_mm = frame_height * 0.2646
        
        # Create PDF with custom size matching frame dimensions
        pdf = FPDF(unit='mm')
        pdf.set_margins(0, 0, 0)  # No margins
        pdf.set_compression(True)  # Enable compression
        
        for idx, frame_file in enumerate(frames):
            try:
                # Add page with frame dimensions
                pdf.add_page(format=(width_mm, height_mm))
                # Add image to fill entire page (same as frame dimensions)
                frame_path = os.path.join(frames_folder, frame_file)
                pdf.image(frame_path, x=0, y=0, w=width_mm, h=height_mm)
                
                # Update progress every 10 frames or every 0.3 seconds
                if progress_callback and task_id and (idx % 10 == 0 or idx == total_frames - 1):
                    progress = 75 + int((idx / total_frames) * 25)
                    progress_callback(task_id, progress, f"Adding frames to PDF... {idx + 1}/{total_frames}")
            except Exception as frame_err:
                logger.warning(f"Failed to add frame {frame_file}: {frame_err}")
                continue
        
        pdf.output(output_path)
        if progress_callback and task_id:
            progress_callback(task_id, 99, "PDF generation complete!")
    except Exception as e:
        logger.error(f"PDF creation failed: {str(e)}")
        raise

# ==================== BACKGROUND TASK ====================
async def background_convert(task_id: str, youtube_url: str, interval_seconds: int):
    conv_logger = get_conversion_logger()
    
    # Track conversion details for final log
    conversion_data = {
        "timestamp": datetime.utcnow().isoformat(),
        "task_id": task_id,
        "youtube_url": youtube_url,
        "video_title": None,
        "video_quality": None,
        "video_size_mb": 0,
        "pdf_created": False,
        "pdf_total_frames": 0,
        "time_interval": interval_seconds,
        "pdf_size_mb": 0,
        "downloaded_by_user": False,
        "conversion_status": "failed"
    }
    
    try:
        task_folder = os.path.join(TEMP_DIR, task_id)
        os.makedirs(task_folder, exist_ok=True)
        
        # Progress callback function with size tracking
        def progress_callback(t_id: str, progress: int, message: str, total_size_mb: float = None, downloaded_size_mb: float = None, download_percentage: float = None, download_threads: int = None):
            TaskManager.update_task(t_id, progress, message, total_size_mb, downloaded_size_mb, download_percentage, download_threads)
        
        # Download video with real-time progress
        TaskManager.update_task(task_id, 1, "Initializing download...")
        video_path = os.path.join(task_folder, 'video.mp4')
        start_download = time.time()
        
        # Run download in executor with progress tracking
        title, video_quality, video_size_mb = await asyncio.get_event_loop().run_in_executor(
            executor, 
            download_video, 
            youtube_url, 
            video_path,
            task_id,
            progress_callback
        )
        download_duration = time.time() - start_download
        
        # Update conversion data
        conversion_data["video_title"] = title
        conversion_data["video_quality"] = video_quality
        conversion_data["video_size_mb"] = round(video_size_mb, 1)
        
        # Extract frames with real-time progress
        frames_folder = os.path.join(task_folder, 'frames')
        os.makedirs(frames_folder, exist_ok=True)
        start_extract = time.time()
        frame_count = await asyncio.get_event_loop().run_in_executor(
            executor, 
            extract_frames, 
            video_path, 
            frames_folder, 
            interval_seconds,
            task_id,
            progress_callback
        )
        extract_duration = time.time() - start_extract
        
        # Generate PDF filename with video title and timestamp
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        # Sanitize title for use in filename
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', title.strip())[:100]
        pdf_filename = f"{safe_title}_{timestamp}.pdf"
        pdf_path = os.path.join(TEMP_DIR, pdf_filename)
        
        # Create PDF with real-time progress
        start_pdf = time.time()
        await asyncio.get_event_loop().run_in_executor(
            executor, 
            create_pdf, 
            frames_folder, 
            pdf_path,
            task_id,
            progress_callback
        )
        pdf_duration = time.time() - start_pdf
        
        pdf_size_mb = 0
        if os.path.exists(pdf_path):
            pdf_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
            conversion_data["pdf_created"] = True
            conversion_data["pdf_total_frames"] = frame_count
            conversion_data["pdf_size_mb"] = round(pdf_size_mb, 1)
        
        # Cleanup temp folder (but keep PDF in TEMP_DIR for download)
        shutil.rmtree(task_folder, ignore_errors=True)
        TaskManager.update_task(task_id, 100, "Conversion complete!")
        TaskManager.complete_task(task_id, pdf_filename)
        
        conversion_data["conversion_status"] = "success"
        
        # Log single comprehensive entry with all details
        conv_logger.info(json.dumps(conversion_data))
        logger.info(f"Task {task_id} completed - PDF: {pdf_filename}")
        
    except Exception as e:
        conversion_data["conversion_status"] = "error"
        conversion_data["error"] = str(e)
        
        # Log single comprehensive entry with error details
        conv_logger.error(json.dumps(conversion_data))
        
        TaskManager.error_task(task_id, str(e))
        logger.error(f"Task {task_id} failed: {str(e)}")
        shutil.rmtree(os.path.join(TEMP_DIR, task_id), ignore_errors=True)

# ==================== API ENDPOINTS ====================
@app.get("/", include_in_schema=False)
async def root():
    return {"message": "YouTube to PDF API", "docs": "/docs", "health": "/health"}

@app.get("/health", tags=["Status"])
async def health():
    """Health check"""
    return {"status": "healthy", "active_tasks": len(active_tasks), "environment": ENVIRONMENT}

@app.get("/convert", tags=["Conversion"], name="convert")
async def convert(youtube_url: str = Query(..., min_length=10, description="YouTube URL"), time_interval: int = Query(60, ge=1, le=3600, description="Seconds between frames")):
    """Start conversion and return related endpoints"""
    try:
        # Validate YouTube URL
        if 'youtube.com' not in youtube_url and 'youtu.be' not in youtube_url:
            raise HTTPException(status_code=400, detail="Invalid YouTube URL")
        
        task_id = str(uuid.uuid4())
        TaskManager.create_task(task_id)
        asyncio.create_task(background_convert(task_id, youtube_url, time_interval))
        return {
            "task_id": task_id,
            "status": "processing",
            "progress": 0,
            "message": "Starting...",
            "created_at": datetime.utcnow().isoformat(),
            "related_endpoints": {
                "progress": f"/progress/{task_id}",
                "stream": f"/stream/{task_id}",
                "cancel": f"/task/{task_id}",
                "download": f"/download/{{pdf_filename}}"
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/progress/{task_id}", tags=["Progress"])
async def get_progress(task_id: str):
    """
    Get task progress with download size information.
    Returns:
    - total_size_mb: Total video size in MB (if available)
    - downloaded_size_mb: Downloaded size in MB (if available)
    - download_percentage: Download completion percentage (if available)
    - progress: Overall task progress (0-100)
    - message: Current status message
    """
    task = TaskManager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # Return task with all fields including size information
    return {
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "progress": task.get("progress", 0),
        "message": task.get("message", ""),
        "total_size_mb": task.get("total_size_mb"),
        "downloaded_size_mb": task.get("downloaded_size_mb"),
        "download_percentage": task.get("download_percentage"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "pdf_filename": task.get("pdf_filename"),
        "completed_at": task.get("completed_at"),
        "download_threads": task.get("download_threads")
    }

@app.get("/stream/{task_id}", tags=["Progress"])
async def progress_stream(task_id: str):
    """
    Real-time progress stream (SSE) with download size information.
    Returns task status including:
    - total_size_mb: Total video size in MB
    - downloaded_size_mb: Downloaded size in MB  
    - download_percentage: Download completion percentage
    - progress: Overall task progress (0-100)
    - message: Current status message
    """
    if not TaskManager.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    
    async def event_generator():
        while True:
            task = TaskManager.get_task(task_id)
            if not task:
                break
            
            # Ensure all size fields are included in response
            task_response = {
                "task_id": task.get("task_id"),
                "status": task.get("status"),
                "progress": task.get("progress", 0),
                "message": task.get("message", ""),
                "total_size_mb": task.get("total_size_mb"),
                "downloaded_size_mb": task.get("downloaded_size_mb"),
                "download_percentage": task.get("download_percentage"),
                "download_threads": task.get("download_threads"),
                "created_at": task.get("created_at"),
                "updated_at": task.get("updated_at"),
                "pdf_filename": task.get("pdf_filename"),
                "completed_at": task.get("completed_at")
            }
            
            yield f"data: {json.dumps(task_response)}\n\n"
            if task.get('status') in ['completed', 'error']:
                break
            await asyncio.sleep(0.5)  # Update every 0.5 seconds for smoother updates
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/download/{pdf_filename}", tags=["Download"])
async def download_pdf(pdf_filename: str):
    """Download PDF"""
    pdf_path = os.path.join(TEMP_DIR, pdf_filename)
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="PDF not found")
    
    # Mark as downloaded
    pdf_downloads[pdf_filename] = True
    
    return FileResponse(pdf_path, media_type='application/pdf', filename=pdf_filename)

@app.get("/tasks", tags=["Status"])
async def list_tasks(status: str = Query(None, description="Filter: processing, completed, error")):
    """List all tasks"""
    tasks = TaskManager.get_all_tasks(status)
    return {"total": len(tasks), "tasks": tasks}

@app.delete("/task/{task_id}", tags=["Status"])
async def cancel_task(task_id: str):
    """Cancel a task"""
    task = TaskManager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.get('status') in ['completed', 'error']:
        raise HTTPException(status_code=400, detail="Cannot cancel completed/failed task")
    TaskManager.error_task(task_id, "Cancelled by user")
    return {"task_id": task_id, "status": "cancelled"}

@app.get("/stats", tags=["Status"])
async def stats():
    """API statistics"""
    all_tasks = task_store.values()
    return {
        "total": len(all_tasks),
        "active": len([t for t in all_tasks if t.get('status') == 'processing']),
        "completed": len([t for t in all_tasks if t.get('status') == 'completed']),
        "failed": len([t for t in all_tasks if t.get('status') == 'error']),
        "max_concurrent": config['max_tasks']
    }

@app.post("/log-download/{pdf_filename}", tags=["Status"], include_in_schema=False)
async def log_download(pdf_filename: str):
    """Internal endpoint to log PDF download for tracking"""
    pdf_downloads[pdf_filename] = True
    return {"status": "logged", "filename": pdf_filename}

# ==================== STARTUP ====================
if __name__ == '__main__':
    import uvicorn
    print(f">>> Starting {ENVIRONMENT} server...")
    print(f"... http://{config['host']}:{config['port']}")
    print(f"... Docs: http://{config['host']}:{config['port']}/docs")
    
    uvicorn.run("main:app", host=config['host'], port=config['port'], reload=config['debug'], log_level="warning")
