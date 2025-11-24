import os
import re
import cv2
os.environ['OPENCV_IO_ENABLE_OPENEXR'] = '0'
import asyncio
from pytubefix import YouTube
from pytubefix.cli import on_progress
from fpdf import FPDF
from PIL import Image
import shutil
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()

# Add CORS middleware for specific domains
allowed_origins = [
    "https://yourdomain.com",
    "http://207.180.210.137",
    "http://localhost:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,  # List of allowed domains
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Thread pool for CPU-bound operations
executor = ThreadPoolExecutor(max_workers=4)

def sanitize_filename(file_name):
    return re.sub(r'[<>:"/\\|?*]', '_', file_name)

def extract_frames(video_path, output_folder, minutes):
    video_capture = cv2.VideoCapture(video_path)
    frame_rate = int(video_capture.get(cv2.CAP_PROP_FPS))
    print("frame rate:", frame_rate)
    total_frames = int(video_capture.get(cv2.CAP_PROP_FRAME_COUNT))
    print("total frame:", total_frames)
    # Calculate frame interval based on minutes and frame rate
    frame_interval = int((frame_rate * 60) * int(minutes))
    print("minutes", minutes)
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

def create_pdf_from_frames(output_folder):
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
    pdf_file_name = f'{output_folder}_frames.pdf'
    pdf.output(pdf_file_name)
    return pdf_file_name

async def download_video_async(yt, video_folder):
    loop = asyncio.get_event_loop()
    video = yt.streams.filter(file_extension='mp4').first()
    if not video:
        return None
    video_extension = video.mime_type.split('/')[-1]
    video_file_name = f'video.{video_extension}'
    # Run blocking download in thread pool
    await loop.run_in_executor(
        executor, 
        video.download, 
        video_folder, 
        video_file_name
    )
    return video_file_name

async def extract_frames_async(video_path, output_folder, minutes):
    loop = asyncio.get_event_loop()
    # Run CPU-intensive frame extraction in thread pool
    await loop.run_in_executor(
        executor,
        extract_frames,
        video_path,
        output_folder,
        minutes
    )

async def create_pdf_async(output_folder):
    loop = asyncio.get_event_loop()
    # Run PDF creation in thread pool
    pdf_file = await loop.run_in_executor(
        executor,
        create_pdf_from_frames,
        output_folder
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

@app.get('/convert_video_to_pdf')
async def convert_video_to_pdf(
    youtube_url: str = Query(..., description="YouTube video URL"),
    time: str = Query(..., description="Time interval in minutes")
):
    try:
        yt = YouTube(youtube_url, use_po_token=True, on_progress_callback=on_progress)
        sanitized_video_id = sanitize_filename(yt.video_id)
        video_folder = f'video_{sanitized_video_id}'
        
        # Create folder asynchronously
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            executor,
            os.makedirs,
            video_folder,
            True
        )
        
        # Download video
        video_file_name = await download_video_async(yt, video_folder)
        
        if not video_file_name:
            raise HTTPException(status_code=404, detail='No downloadable video found')
        
        # Extract frames
        video_path = os.path.join(video_folder, video_file_name)
        await extract_frames_async(video_path, video_folder, time)
        
        # Create PDF
        pdf_file = await create_pdf_async(video_folder)
        
        # Cleanup video folder
        await cleanup_folder_async(video_folder)
        
        # Return PDF file
        return FileResponse(
            pdf_file, 
            media_type='application/pdf',
            filename=os.path.basename(pdf_file)
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
