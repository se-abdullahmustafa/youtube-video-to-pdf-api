# YouTube Video to PDF Converter API

A high-performance FastAPI service that converts YouTube videos to PDF by extracting frames at specified intervals using yt-dlp for reliable video downloads.

## Features

- Convert YouTube videos to PDF documents using yt-dlp for reliable downloads
- Configurable frame extraction intervals (in minutes)
- Asynchronous processing for better performance
- Built with FastAPI for high performance and automatic API documentation
- CORS enabled for web application integration
- Clean and efficient frame extraction using OpenCV
- PDF generation with proper image scaling and formatting
- Automatic cleanup of temporary files
- Support for various YouTube URL formats
- Proper error handling and validation

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
   python fastapi_app.py
   ```

2. The API will be available at `http://127.0.0.1:8000`

## API Documentation

Once the server is running, you can access the interactive API documentation:

- **Swagger UI**: http://127.0.0.1:8000/docs
- **ReDoc**: http://127.0.0.1:8000/redoc

## API Endpoints

### Convert YouTube Video to PDF

```
GET /convert_video_to_pdf
```

**Parameters:**

- `youtube_url` (required): The URL of the YouTube video to convert (supports various YouTube URL formats)
- `time` (required): Time interval in minutes between frames (must be a positive integer)

**Example Request:**

```
GET /convert_video_to_pdf?youtube_url=https://www.youtube.com/watch?v=example&time=1
```

**Response:**

- Returns the generated PDF file for download with the video title as the filename
- Proper MIME type: `application/pdf`
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
