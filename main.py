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
ENV_CONFIG = {
    'local': {'host': '0.0.0.0', 'port': 8000, 'debug': True, 'max_workers': 8, 'max_tasks': 500, 'cleanup_hours': 24},
    'production': {'host': '0.0.0.0', 'port': 80, 'debug': False, 'max_workers': 64, 'max_tasks': 5000, 'cleanup_hours': 12}
}
config = ENV_CONFIG.get(ENVIRONMENT, ENV_CONFIG['local'])

# Proxy configuration from .env file for both environments
PROXY_URL = os.getenv('PROXY_URL', '')

TEMP_DIR = "temp"
LOGS_DIR = "logs"
CLEANUP_INTERVAL = 3600  # 1 hour in seconds
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

# ==================== APP SETUP ====================
app = FastAPI(
    title="YouTube to PDF API",
    version="2.0.0",
    description="Fast & reliable YouTube to PDF converter",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json"
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
    def update_task(cls, task_id: str, progress: int, message: str):
        with task_lock:
            if task_id in task_store:
                task_store[task_id].update({"progress": progress, "message": message, "updated_at": datetime.utcnow().isoformat()})
    
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
def download_video(youtube_url: str, output_path: str) -> str:
    try:
        # Format: best video at max 480p with audio, fallback to best available
        opts = {
            'format': 'best[height<=480][ext=mp4]/best[height<=480]/best[ext=mp4]',
            'outtmpl': output_path,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'http_chunk_size': 1024 * 1024,  # 1MB chunks
            'concurrent_fragment_downloads': 4,
            'retries': 3
        }
        # Only add proxy if configured
        if PROXY_URL:
            opts['proxy'] = PROXY_URL
        
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            return info.get('title', 'video')
    except Exception as e:
        logger.error(f"Download failed: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Failed to download: {str(e)[:100]}")

def extract_frames(video_path: str, output_folder: str, interval_seconds: int) -> int:
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise Exception("Cannot open video")
        
        fps = int(cap.get(cv2.CAP_PROP_FPS))
        frame_interval = max(1, fps * interval_seconds)
        frame_count = 0
        frame_idx = 0
        
        while True:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            success, frame = cap.read()
            if not success:
                break
            
            # Add timestamp to top-right corner
            timestamp_seconds = int(frame_idx / fps)
            hours = timestamp_seconds // 3600
            minutes = (timestamp_seconds % 3600) // 60
            seconds = timestamp_seconds % 60
            timestamp_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            
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
        
        cap.release()
        return frame_count
    except Exception as e:
        logger.error(f"Frame extraction failed: {str(e)}")
        raise

def create_pdf(frames_folder: str, output_path: str):
    try:
        frames = sorted([f for f in os.listdir(frames_folder) if f.endswith('.jpg')])
        if not frames:
            raise Exception("No frames extracted")
        
        # Create PDF with A4 size in landscape orientation
        pdf = FPDF(orientation='L', unit='mm', format='A4')
        pdf.set_margins(0, 0, 0)  # No margins
        pdf.set_compression(True)  # Enable compression
        
        # A4 landscape dimensions: 297mm width × 210mm height
        for frame_file in frames:
            try:
                pdf.add_page()
                # Add image to fill entire A4 landscape page
                frame_path = os.path.join(frames_folder, frame_file)
                pdf.image(frame_path, x=0, y=0, w=297, h=210)
            except Exception as frame_err:
                logger.warning(f"Failed to add frame {frame_file}: {frame_err}")
                continue
        
        pdf.output(output_path)
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
        
        # Download video
        TaskManager.update_task(task_id, 20, "Downloading...")
        video_path = os.path.join(task_folder, 'video.mp4')
        start_download = time.time()
        title = await asyncio.get_event_loop().run_in_executor(executor, download_video, youtube_url, video_path)
        download_duration = time.time() - start_download
        
        video_quality = "480p"  # Based on format selection in download_video
        video_size_mb = 0
        
        if os.path.exists(video_path):
            video_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            conversion_data["video_title"] = title
            conversion_data["video_quality"] = video_quality
            conversion_data["video_size_mb"] = round(video_size_mb, 1)
        
        # Extract frames
        TaskManager.update_task(task_id, 50, "Extracting frames...")
        frames_folder = os.path.join(task_folder, 'frames')
        os.makedirs(frames_folder, exist_ok=True)
        start_extract = time.time()
        frame_count = await asyncio.get_event_loop().run_in_executor(executor, extract_frames, video_path, frames_folder, interval_seconds)
        extract_duration = time.time() - start_extract
        
        # Create PDF
        TaskManager.update_task(task_id, 80, f"Creating PDF ({frame_count} frames)...")
        
        # Generate PDF filename with video title and timestamp
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        # Sanitize title for use in filename
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', title.strip())[:100]
        pdf_filename = f"{safe_title}_{timestamp}.pdf"
        pdf_path = os.path.join(TEMP_DIR, pdf_filename)
        
        start_pdf = time.time()
        await asyncio.get_event_loop().run_in_executor(executor, create_pdf, frames_folder, pdf_path)
        pdf_duration = time.time() - start_pdf
        
        pdf_size_mb = 0
        if os.path.exists(pdf_path):
            pdf_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
            conversion_data["pdf_created"] = True
            conversion_data["pdf_total_frames"] = frame_count
            conversion_data["pdf_size_mb"] = round(pdf_size_mb, 1)
        
        # Cleanup temp folder (but keep PDF in TEMP_DIR for download)
        shutil.rmtree(task_folder, ignore_errors=True)
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

# Track PDF downloads
pdf_downloads: Dict[str, bool] = {}

# ==================== STARTUP ====================
@app.on_event("startup")
async def startup_event():
    """Initialize background cleanup task"""
    logger.info(f"Starting server in {ENVIRONMENT} mode with max {config['max_tasks']} concurrent tasks")
    asyncio.create_task(cleanup_task())

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
    """Get task progress"""
    task = TaskManager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.get("/stream/{task_id}", tags=["Progress"])
async def progress_stream(task_id: str):
    """Real-time progress (SSE)"""
    if not TaskManager.get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    
    async def event_generator():
        while True:
            task = TaskManager.get_task(task_id)
            if not task:
                break
            yield f"data: {json.dumps(task)}\n\n"
            if task.get('status') in ['completed', 'error']:
                break
            await asyncio.sleep(1)
    
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
