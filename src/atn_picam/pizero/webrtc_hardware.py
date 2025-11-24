#!/usr/bin/env python3
"""
Raspberry Pi Camera WebRTC Streamer
-----------------------------------
This script implements a low-latency WebRTC streaming solution for the Raspberry Pi.
It uses:
- aiortc: For the WebRTC stack (Python)
- aiohttp: For the signaling web server
- picamera2: For capturing frames from the Pi Camera

Prerequisites:
    pip install aiohttp aiortc opencv-python-headless av

Note: On Pi Zero 2W, software encoding (H.264/VP8) can be CPU intensive.
"""

import argparse
import asyncio
import json
import logging
import os
import platform
import time
import uuid
import psutil
import subprocess
import io
from datetime import datetime

from aiohttp import web
from atn_picam.core.storage import check_and_cleanup_storage

# Try importing Picamera2
try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FileOutput
    HAVE_PICAM2 = True
except ImportError:
    HAVE_PICAM2 = False
    print("Warning: picamera2 not found. Will try to use standard video device if available.")

class SplitterOutput(io.BufferedIOBase):
    """
    Custom output that can write to a file and/or a pipe simultaneously.
    Used to share a single H.264 encoder between recording and streaming.
    """
    def __init__(self):
        self.file = None
        self.pipe = None
        self.lock = asyncio.Lock() # Not strictly needed for synchronous write, but good practice

    def writable(self):
        return True

    def set_file(self, file_path):
        if self.file:
            self.file.close()
        self.file = open(file_path, "wb")

    def close_file(self):
        if self.file:
            self.file.close()
            self.file = None

    def set_pipe(self, pipe):
        self.pipe = pipe

    def close_pipe(self):
        self.pipe = None

    def write(self, data):
        # Write to file if active
        if self.file:
            self.file.write(data)
        
        # Write to pipe if active
        if self.pipe:
            try:
                self.pipe.write(data)
                self.pipe.flush()
            except (BrokenPipeError, IOError):
                # Pipe closed unexpectedly
                self.pipe = None

    def flush(self):
        if self.file:
            self.file.flush()
        if self.pipe:
            self.pipe.flush()

    def close(self):
        self.close_file()
        self.close_pipe()

class CameraManager:
    """
    Singleton class to manage the Picamera2 instance.
    Handles dual streams:
    - 'main': High resolution (1280x720) for H.264 recording AND streaming
    - 'lores': Low resolution (640x360) for legacy aiortc preview
    """
    def __init__(self):
        self.picam2 = None
        self.recording_active = False
        self.streaming_active = False
        self.current_recording_file = None
        self.h264_encoder = None
        self.splitter_output = SplitterOutput()
        self.stream_process = None
        self.running = False
        
        if HAVE_PICAM2:
            self._init_camera()

    def _init_camera(self):
        print("Initializing Camera Manager...")
        self.picam2 = Picamera2()
        
        # Configure stream
        # We use 1280x720 for the main H.264 stream to ensure stability on Pi Zero 2 W
        # when doing both recording and streaming.
        config = self.picam2.create_video_configuration(
            main={"size": (1280, 720), "format": "YUV420"},
            controls={"FrameRate": 30.0}
        )
        self.picam2.configure(config)
        self.picam2.start()
        self.running = True
        
        # Warmup
        time.sleep(1)
        print("Camera Manager ready.")

    def _ensure_encoder_running(self):
        """Start the shared H.264 encoder if it's not already running"""
        if self.h264_encoder is None:
            print("Starting shared H.264 encoder...")
            self.h264_encoder = H264Encoder(
                bitrate=4000000,   # 4 Mbps (Compromise for Stream+Record)
                repeat=True,       # Required for streaming
                iperiod=30,        # 1 keyframe/sec
                framerate=30
            )
            self.picam2.start_encoder(self.h264_encoder, FileOutput(self.splitter_output), name="main")

    def _check_stop_encoder(self):
        """Stop the shared encoder if no outputs are active"""
        if not self.recording_active and not self.streaming_active:
            if self.h264_encoder:
                print("Stopping shared H.264 encoder...")
                self.picam2.stop_encoder(self.h264_encoder)
                self.h264_encoder = None

    def start_streaming(self):
        """Start streaming to mediamtx via ffmpeg"""
        if not self.running:
            return {"success": False, "message": "Camera not running"}
        if self.streaming_active:
            return {"success": False, "message": "Streaming already active"}

        try:
            # Start ffmpeg to push to mediamtx (RTSP)
            cmd = [
                'ffmpeg',
                '-f', 'h264',
                '-i', 'pipe:0',
                '-c:v', 'copy',
                '-f', 'rtsp',
                'rtsp://localhost:8554/cam'
            ]
            
            print(f"Starting ffmpeg: {' '.join(cmd)}")
            self.stream_process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            
            # Attach pipe to splitter
            self.splitter_output.set_pipe(self.stream_process.stdin)
            
            self.streaming_active = True
            self._ensure_encoder_running()
            
            print("Started streaming to mediamtx")
            return {"success": True}
            
        except Exception as e:
            print(f"Start streaming failed: {e}")
            self.stop_streaming()
            return {"success": False, "message": str(e)}

    def stop_streaming(self):
        """Stop streaming to mediamtx"""
        if not self.streaming_active:
            return {"success": False, "message": "No active stream"}
            
        try:
            self.streaming_active = False
            self.splitter_output.close_pipe()
            
            if self.stream_process:
                if self.stream_process.stdin:
                    self.stream_process.stdin.close()
                self.stream_process.terminate()
                self.stream_process.wait(timeout=2)
                self.stream_process = None
            
            self._check_stop_encoder()
            print("Stopped streaming")
            return {"success": True}
            
        except Exception as e:
            print(f"Stop streaming failed: {e}")
            return {"success": False, "message": str(e)}

    def start_recording(self):
        if not self.running:
            return {"success": False, "message": "Camera not running"}
        if self.recording_active:
            return {"success": False, "message": "Recording already active"}
        
        try:
            recordings_dir = os.path.expanduser("~/recordings")
            os.makedirs(recordings_dir, exist_ok=True)
            
            check_and_cleanup_storage(recordings_dir)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(recordings_dir, f"webrtc_{timestamp}.h264")
            
            # Attach file to splitter
            self.splitter_output.set_file(filename)
            
            self.recording_active = True
            self.current_recording_file = filename
            self._ensure_encoder_running()
            
            print(f"Started recording to {filename}")
            return {"success": True, "file": filename}
            
        except Exception as e:
            print(f"Start recording failed: {e}")
            self.recording_active = False
            return {"success": False, "message": str(e)}

    def stop_recording(self):
        if not self.recording_active:
            return {"success": False, "message": "No active recording"}
        
        filename = self.current_recording_file
        try:
            self.recording_active = False
            self.splitter_output.close_file()
            self.current_recording_file = None
            
            self._check_stop_encoder()
            
            print(f"Stopped recording: {filename}")
            return {"success": True, "file": filename}
            
        except Exception as e:
            print(f"Stop recording failed: {e}")
            return {"success": False, "message": str(e)}

    def get_status(self):
        return {
            "running": self.running,
            "recording": self.recording_active,
            "streaming": self.streaming_active,
            "file": self.current_recording_file
        }

    def close(self):
        if self.recording_active:
            self.stop_recording()
        if self.streaming_active:
            self.stop_streaming()
        if self.picam2 and self.running:
            self.picam2.stop()
            self.picam2.close()
            self.running = False

# Global camera manager instance
camera_manager = CameraManager()

def get_template_path(filename):
    """Get absolute path to template file"""
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates', filename)

async def index(request):
    with open(get_template_path('webrtc_hardware_index.html'), 'r') as f:
        content = f.read()
    return web.Response(content_type="text/html", text=content)

async def embed(request):
    with open(get_template_path('webrtc_embed.html'), 'r') as f:
        content = f.read()
    return web.Response(content_type="text/html", text=content)

async def start_streaming(request):
    result = camera_manager.start_streaming()
    return web.json_response(result)

async def stop_streaming(request):
    result = camera_manager.stop_streaming()
    return web.json_response(result)

async def start_recording(request):
    result = camera_manager.start_recording()
    return web.json_response(result)

async def stop_recording(request):
    result = camera_manager.stop_recording()
    return web.json_response(result)

async def storage_info(request):
    try:
        recordings_dir = os.path.expanduser("~/recordings")
        os.makedirs(recordings_dir, exist_ok=True)
        
        stat = os.statvfs(recordings_dir)
        free_space_gb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024 * 1024)
        total_space_gb = (stat.f_blocks * stat.f_frsize) / (1024 * 1024 * 1024)
        
        return web.json_response({
            "success": True,
            "free_gb": round(free_space_gb, 2),
            "total_gb": round(total_space_gb, 2)
        })
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)})

async def status(request):
    return web.json_response(camera_manager.get_status())

async def monitor_loop():
    """Background task to monitor CPU and Network usage"""
    process = psutil.Process()
    old_net_io = psutil.net_io_counters()
    last_time = time.time()
    
    print("[Monitor] System monitoring started...")
    
    while True:
        try:
            await asyncio.sleep(3)
            
            current_time = time.time()
            dt = current_time - last_time
            
            # CPU (interval=None is non-blocking if called periodically)
            cpu_percent = psutil.cpu_percent()
            
            # Network
            net_io = psutil.net_io_counters()
            bytes_sent = net_io.bytes_sent - old_net_io.bytes_sent
            bytes_recv = net_io.bytes_recv - old_net_io.bytes_recv
            
            mbps_sent = (bytes_sent * 8) / (1000 * 1000 * dt) # Mbps (Megabits)
            mbps_recv = (bytes_recv * 8) / (1000 * 1000 * dt)
            
            print(f"[System] CPU: {cpu_percent:5.1f}% | Net Up: {mbps_sent:5.2f} Mbps | Net Down: {mbps_recv:5.2f} Mbps")
            
            old_net_io = net_io
            last_time = current_time
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Monitor] Error: {e}")

async def on_startup(app):
    app['monitor_task'] = asyncio.create_task(monitor_loop())

async def on_shutdown(app):
    # Cancel monitor task
    if 'monitor_task' in app:
        app['monitor_task'].cancel()
        try:
            await app['monitor_task']
        except asyncio.CancelledError:
            pass

    # Close camera
    if camera_manager:
        camera_manager.close()

def main():
    parser = argparse.ArgumentParser(description="WebRTC Streamer for Pi Camera")
    parser.add_argument("--port", type=int, default=8080, help="Port for web server (default: 8080)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host for web server (default: 0.0.0.0)")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO)

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/embed", embed)
    
    # Add recording and streaming API routes
    app.router.add_post("/start_streaming", start_streaming)
    app.router.add_post("/stop_streaming", stop_streaming)
    app.router.add_post("/start_recording", start_recording)
    app.router.add_post("/stop_recording", stop_recording)
    app.router.add_get("/storage_info", storage_info)
    app.router.add_get("/status", status)

    print(f"Starting WebRTC server at http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
