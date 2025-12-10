import os, re, cv2, yt_dlp, uuid, asyncio, logging, json, shutil, time
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor
from fpdf import FPDF
from fastapi import FastAPI, Query, HTTPException, status, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

# ==================== CONFIG ====================
ENVIRONMENT = os.getenv('ENVIRONMENT', 'local').lower()
ENV_CONFIG = {
    'local': {'host': '0.0.0.0', 'port': 8000, 'debug': True, 'max_workers': 8, 'max_tasks': 500},
    'production': {'host': '0.0.0.0', 'port': 80, 'debug': False, 'max_workers': 32, 'max_tasks': 2000}
}
config = ENV_CONFIG.get(ENVIRONMENT, ENV_CONFIG['local'])

TEMP_DIR = "temp"
LOGS_DIR = "logs"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# ==================== LOGGING ====================
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
    
    # Create formatter for conversion logs
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    # File handler for unified conversion log
    log_file = os.path.join(LOGS_DIR, 'conversions.log')
    file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setFormatter(formatter)
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

class TaskManager:
    @classmethod
    def create_task(cls, task_id: str):
        if len(active_tasks) >= config['max_tasks']:
            raise HTTPException(status_code=429, detail="Server busy")
        task_store[task_id] = {"status": "processing", "progress": 0, "message": "Starting...", "created_at": datetime.utcnow().isoformat()}
        active_tasks.add(task_id)
    
    @classmethod
    def update_task(cls, task_id: str, progress: int, message: str):
        if task_id in task_store:
            task_store[task_id].update({"progress": progress, "message": message})
    
    @classmethod
    def complete_task(cls, task_id: str, pdf_filename: str):
        if task_id in task_store:
            task_store[task_id].update({"status": "completed", "progress": 100, "message": "Done", "pdf_filename": pdf_filename})
            active_tasks.discard(task_id)
    
    @classmethod
    def error_task(cls, task_id: str, error: str):
        if task_id in task_store:
            task_store[task_id].update({"status": "error", "progress": 0, "message": f"Error: {error[:50]}"})
            active_tasks.discard(task_id)
    
    @classmethod
    def get_task(cls, task_id: str):
        return task_store.get(task_id)
    
    @classmethod
    def get_all_tasks(cls, status: str = None):
        tasks = list(task_store.values())
        return [t for t in tasks if t.get('status') == status] if status else tasks

# ==================== VIDEO PROCESSING ====================
def download_video(youtube_url: str, output_path: str) -> str:
    try:
        opts = {'format': 'best[ext=mp4]', 'outtmpl': output_path, 'quiet': True, 'no_warnings': True}
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
            cv2.imwrite(os.path.join(output_folder, f'frame_{frame_count:04d}.jpg'), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
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
        
        pdf = FPDF(orientation='P', unit='mm', format='A4')
        for frame_file in frames:
            try:
                pdf.add_page()
                pdf.image(os.path.join(frames_folder, frame_file), x=10, y=10, w=190)
            except:
                continue
        
        pdf.output(output_path)
    except Exception as e:
        logger.error(f"PDF creation failed: {str(e)}")
        raise

# ==================== BACKGROUND TASK ====================
async def background_convert(task_id: str, youtube_url: str, interval_minutes: int):
    conv_logger = get_conversion_logger()
    
    try:
        conv_logger.info(f"===== CONVERSION STARTED =====")
        conv_logger.info(f"Task ID: {task_id}")
        conv_logger.info(f"YouTube URL: {youtube_url}")
        conv_logger.info(f"Frame interval: {interval_minutes} minute(s)")
        
        task_folder = os.path.join(TEMP_DIR, task_id)
        os.makedirs(task_folder, exist_ok=True)
        conv_logger.info(f"Task folder created: {task_folder}")
        
        # Download video
        conv_logger.info(f"[STEP 1/4] Downloading video...")
        TaskManager.update_task(task_id, 20, "Downloading...")
        video_path = os.path.join(task_folder, 'video.mp4')
        start_download = time.time()
        title = await asyncio.get_event_loop().run_in_executor(executor, download_video, youtube_url, video_path)
        download_duration = time.time() - start_download
        
        if os.path.exists(video_path):
            video_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            conv_logger.info(f"Video downloaded successfully")
            conv_logger.info(f"  Title: {title}")
            conv_logger.info(f"  Size: {video_size_mb:.2f} MB")
            conv_logger.info(f"  Duration: {download_duration:.2f}s")
        
        # Extract frames
        conv_logger.info(f"[STEP 2/4] Extracting frames at {interval_minutes} minute interval...")
        TaskManager.update_task(task_id, 50, "Extracting frames...")
        frames_folder = os.path.join(task_folder, 'frames')
        os.makedirs(frames_folder, exist_ok=True)
        start_extract = time.time()
        frame_count = await asyncio.get_event_loop().run_in_executor(executor, extract_frames, video_path, frames_folder, interval_minutes * 60)
        extract_duration = time.time() - start_extract
        
        conv_logger.info(f"Frames extracted successfully")
        conv_logger.info(f"  Total frames: {frame_count}")
        conv_logger.info(f"  Duration: {extract_duration:.2f}s")
        
        # Create PDF
        conv_logger.info(f"[STEP 3/4] Creating PDF document...")
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
        
        if os.path.exists(pdf_path):
            pdf_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
            conv_logger.info(f"PDF created successfully")
            conv_logger.info(f"  Filename: {pdf_filename}")
            conv_logger.info(f"  Size: {pdf_size_mb:.2f} MB")
            conv_logger.info(f"  Duration: {pdf_duration:.2f}s")
        
        # Cleanup and complete
        conv_logger.info(f"[STEP 4/4] Cleaning up and finalizing...")
        shutil.rmtree(task_folder, ignore_errors=True)
        TaskManager.complete_task(task_id, pdf_filename)
        
        total_duration = download_duration + extract_duration + pdf_duration
        conv_logger.info(f"===== CONVERSION COMPLETED SUCCESSFULLY =====")
        conv_logger.info(f"Total time: {total_duration:.2f}s")
        conv_logger.info(f"Output: {pdf_filename}")
        logger.info(f"Task {task_id} completed - PDF: {pdf_filename}")
        
    except Exception as e:
        conv_logger.error(f"===== CONVERSION FAILED =====")
        conv_logger.error(f"Error: {str(e)}")
        conv_logger.error(f"Exception type: {type(e).__name__}", exc_info=True)
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
async def convert(youtube_url: str = Query(..., description="YouTube URL"), time_interval: int = Query(1, ge=1, le=60, description="Minutes between frames")):
    """Start conversion and return related endpoints"""
    try:
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

# ==================== STARTUP ====================
if __name__ == '__main__':
    import uvicorn
    print(f">>> Starting {ENVIRONMENT} server...")
    print(f"... http://{config['host']}:{config['port']}")
    print(f"... Docs: http://{config['host']}:{config['port']}/docs")
    
    uvicorn.run("main:app", host=config['host'], port=config['port'], reload=config['debug'], log_level="warning")
