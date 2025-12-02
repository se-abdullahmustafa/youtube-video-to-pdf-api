# YouTube Video to PDF Converter API

A high-performance FastAPI service that converts YouTube videos to PDF by extracting frames at specified intervals using yt-dlp for reliable video downloads.

## Features

- Convert YouTube videos to PDF documents using yt-dlp for reliable downloads
- Configurable frame extraction intervals (in seconds)
- Real-time progress tracking with Server-Sent Events (SSE)
- Asynchronous processing for better performance
- Built with FastAPI for high performance and automatic API documentation
- CORS enabled for web application integration
- Clean and efficient frame extraction using OpenCV
- PDF generation with proper image scaling and formatting
- Automatic cleanup of temporary files
- Support for various YouTube URL formats
- Production-grade error handling and validation
- Dedicated temp folder for downloads and conversions

## Prerequisites

- Python 3.8+
- pip (Python package manager)
- FFmpeg (for video processing)
  - On Windows: Download from [FFmpeg's official site](https://ffmpeg.org/download.html) and add to PATH
  - On macOS: `brew install ffmpeg`
  - On Ubuntu/Debian: `sudo apt-get install ffmpeg`

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/yourusername/youtube-video-to-pdf-api.git
   cd youtube-video-to-pdf-api
   ```

2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Running the API

1. Start the FastAPI server:

   ```bash
   python main.py
   ```

2. The API will be available at `http://127.0.0.1:8000`

## Project Structure

```
youtube-video-to-pdf-api/
├── main.py                 # Main FastAPI application
├── requirements.txt        # Python dependencies
├── README.md              # This file
├── .gitignore             # Git ignore file
├── .env.local             # Local environment config
├── .env.staging           # Staging environment config
├── .env.production        # Production environment config
├── temp/                  # Temporary folder for downloads and conversions
│   └── temp_video_*/      # Individual conversion folders
│       ├── video.mp4      # Downloaded YouTube video
│       ├── frame_*.jpg    # Extracted frames
│       └── video_title.pdf # Generated PDF
└── logs/                  # Logs folder
    └── app.log            # Application logs
```

## Environments

This application supports three environments:

### Local Development

- **Frontend**: http://localhost:3000
- **API**: http://localhost:8001
- **Environment file**: `.env.local`
- **Features**: Debug mode enabled, hot reload

### Staging

- **Frontend**: https://staging.ytglancer.com
- **API**: Port 8000
- **Environment file**: `.env.staging`
- **Features**: Debug mode enabled, testing environment

### Production

- **Frontend**: https://ytglancer.com
- **API**: Port 8000
- **Environment file**: `.env.production`
- **Features**: Optimized for performance, no debug mode

### Running in Different Environments

```bash
# Local (default)
python main.py

# Set environment explicitly
ENVIRONMENT=staging python main.py
ENVIRONMENT=production python main.py

# Or use environment files
cp .env.staging .env
python main.py
```

## API Documentation

Once the server is running, you can access the interactive API documentation:

- **Swagger UI**: http://127.0.0.1:8000/docs
- **ReDoc**: http://127.0.0.1:8000/redoc

## API Endpoints

### Convert YouTube Video to PDF

**Endpoint**: `GET /convert_video_to_pdf`

**Parameters**:

- `youtube_url` (string, required): YouTube video URL
- `time_interval` (integer, required): Time interval in seconds between frames (1-3600)

**Response**: Returns a task ID for progress tracking

**Example**:

```bash
curl "http://127.0.0.1:8000/convert_video_to_pdf?youtube_url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&time_interval=30"
```

### Progress Tracking

**Get Progress**: `GET /progress/{task_id}`

**Real-time Progress Stream**: `GET /progress-stream/{task_id}`

**Download PDF**: `GET /download/{pdf_filename}`

**Progress Steps**:

1. `downloading` (10-30%): Downloading YouTube video
2. `extracting` (50%): Extracting frames from video
3. `building_pdf` (80%): Building PDF document
4. `done` (95-100%): Finalizing and completing

**JavaScript Example for Real-time Progress**:

```javascript
// Start conversion
const response = await fetch(
  "/convert_video_to_pdf?youtube_url=URL&time_interval=30"
);
const { task_id, progress_stream_url } = await response.json();

// Listen to real-time progress
const eventSource = new EventSource(progress_stream_url);
eventSource.onmessage = (event) => {
  const progress = JSON.parse(event.data);
  console.log(progress.step, progress.progress, progress.message);

  if (progress.step === "completed") {
    window.location.href = progress.pdf_url;
  }
};
```

**Response:**

- Returns the generated PDF file for download with the video title as the filename
- Content-Disposition header for proper download handling

## Environment Variables

## Configuration

The following environment variables can be configured:

- `OPENCV_IO_ENABLE_OPENEXR`: Set to '0' to disable OpenEXR support (default)
- `PORT`: Port to run the server on (default: 8000)
- `HOST`: Host to bind the server to (default: 0.0.0.0)
- `TEMP_DIR`: Directory to store temporary files (default: system temp directory)

## Development

### Project Structure

- `fastapi_app.py`: Main FastAPI application with yt-dlp integration
- `requirements.txt`: Project dependencies including yt-dlp
- `README.md`: This documentation file

### Dependencies

- `fastapi`: Web framework for building the API
- `uvicorn`: ASGI server for running FastAPI
- `yt-dlp`: Feature-rich command-line program to download videos from YouTube and other sites
- `opencv-python-headless`: For video processing and frame extraction
- `Pillow`: For image processing
- `fpdf`: For PDF generation

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-asyncio httpx

# Run tests
pytest
```

## Deployment

### Using Uvicorn

```bash
uvicorn fastapi_app:app --host 0.0.0.0 --port 8000 --workers 4
```

### Using Gunicorn (for production)

```bash
gunicorn -w 4 -k uvicorn.workers.UvicornWorker fastapi_app:app
```

## Contributing

1. Fork the repository
2. Create a new branch for your feature
3. Commit your changes
4. Push to the branch
5. Create a new Pull Request

## Troubleshooting

### Common Issues

1. **FFmpeg not found**:
   Ensure FFmpeg is installed and available in your system PATH.
2. **Video download failures**:

   - Check your internet connection
   - Verify the YouTube URL is correct and accessible
   - Some videos might be age-restricted or have download restrictions

3. **Memory issues with large videos**:
   - Increase the time interval between frames
   - Consider using a server with more RAM for processing large videos

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [FastAPI](https://fastapi.tiangolo.com/) for the awesome web framework
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) for reliable YouTube downloads
- [OpenCV](https://opencv.org/) for video processing
- [FPDF](https://pyfpdf.readthedocs.io/) for PDF generation

## Acknowledgments

- FastAPI for the awesome web framework
- OpenCV for video processing
- pytubefix for YouTube video downloading
- FPDF for PDF generation
