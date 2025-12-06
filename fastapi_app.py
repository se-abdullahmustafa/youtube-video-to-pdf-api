import os
import re
import cv2

os.environ['OPENCV_IO_ENABLE_OPENEXR'] = '0'
import asyncio
import yt_dlp
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


def download_video_with_ytdlp(youtube_url, video_folder):
    """Download video using yt-dlp"""
    video_file_name = 'video.mp4'
    output_path = os.path.join(video_folder, video_file_name)

    ydl_opts = {
        'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
        'outtmpl': output_path,
        'quiet': False,
        'no_warnings': False,
        'merge_output_format': 'mp4',
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 10,
        'extractor_retries': 10,
        'socket_timeout': 60,
        'http_chunk_size': 10485760,  # 10MB chunks
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android'],
                'skip': ['dash', 'hls']
            }
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([youtube_url])

    return video_file_name


def get_video_id_from_url(youtube_url):
    """Extract video ID from YouTube URL"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
        return info.get('id', 'video')


async def download_video_async(youtube_url, video_folder):
    loop = asyncio.get_event_loop()
    # Run blocking download in thread pool
    video_file_name = await loop.run_in_executor(
        executor,
        download_video_with_ytdlp,
        youtube_url,
        video_folder
    )
    return video_file_name


async def get_video_id_async(youtube_url):
    loop = asyncio.get_event_loop()
    # Run video ID extraction in thread pool
    video_id = await loop.run_in_executor(
        executor,
        get_video_id_from_url,
        youtube_url
    )
    return video_id


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
    video_folder = ''
    try:
        # Get video ID
        video_id = await get_video_id_async(youtube_url)
        sanitized_video_id = sanitize_filename(video_id)
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
        video_file_name = await download_video_async(youtube_url, video_folder)

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
        # Cleanup video folder
        if video_folder and video_folder != '':
            await cleanup_folder_async(video_folder)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
