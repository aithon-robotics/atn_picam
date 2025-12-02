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
from datetime import datetime

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaPlayer
import av
from atn_picam.core.storage import check_and_cleanup_storage

# CORS middleware to allow cross-origin requests
@web.middleware
async def cors_middleware(request, handler):
    """Add CORS headers to all responses"""
    if request.method == 'OPTIONS':
        # Handle preflight requests
        response = web.Response()
    else:
        response = await handler(request)
    
    # Add CORS headers
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Max-Age'] = '3600'
    
    return response

# Try importing Picamera2
try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FileOutput, Output
    HAVE_PICAM2 = True
except ImportError:
    HAVE_PICAM2 = False
    Output = object  # Fallback
    print("Warning: picamera2 not found. Will try to use standard video device if available.")

class PipeOutput(Output if HAVE_PICAM2 else object):
    """
    Output class that pipes data to a subprocess (e.g. ffmpeg).
    Must inherit from picamera2.outputs.Output for compatibility.
    """
    def __init__(self, command):
        if HAVE_PICAM2:
            super().__init__()
        print(f"Starting subprocess: {' '.join(command)}")
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE)

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=None):
        """Called by picamera2 encoder - writes frame data to subprocess"""
        return self.write(frame)

    def write(self, data):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(data)
                return len(data)
            except Exception as e:
                print(f"PipeOutput write error: {e}")
        return 0

    def close(self):
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()
    
    def flush(self):
        if self.proc.stdin:
            self.proc.stdin.flush()

class CameraManager:
    """
    Singleton class to manage the Picamera2 instance.
    Handles dual streams:
    - 'main': High resolution (1920x1080) for H.264 recording
    - 'lores': Low resolution (640x480) for WebRTC streaming
    """
    def __init__(self):
        self.picam2 = None
        self.recording_active = False
        self.current_recording_file = None
        self.h264_encoder = None
        self.output = None
        self.running = False
        
        if HAVE_PICAM2:
            self._init_camera()

    def _init_camera(self):
        print("Initializing Camera Manager...")
        self.picam2 = Picamera2()
        
        # Configure dual streams
        # Camera Module 3 (IMX708) is native 16:9 (4608x2592)
        # We use 16:9 for both streams to avoid cropping/distortion
        config = self.picam2.create_video_configuration(
            main={"size": (1920, 1080), "format": "YUV420"},  # High quality for recording
            lores={"size": (960, 540), "format": "YUV420"},   # Low res for WebRTC (qHD)
            controls={"FrameRate": 24.0}
        )
        self.picam2.configure(config)
        self.picam2.start()
        self.running = True
        
        # Warmup
        time.sleep(1)
        print("Camera Manager ready.")

    def get_frame(self):
        """Capture frame from lores stream for WebRTC"""
        if not self.running:
            return None
        # Capture from the low-res stream
        return self.picam2.capture_array("lores")

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
            filename = os.path.join(recordings_dir, f"webrtc_{timestamp}.mp4")
            
            # Create H.264 encoder attached to the MAIN (high-res) stream
            # Lower bitrate to 5Mbps to avoid SD card bottlenecks on Pi Zero 2W
            self.h264_encoder = H264Encoder(
                bitrate=5000000, # 5 Mbps
                repeat=True,
                iperiod=24, # Match framerate
                framerate=24
            )
            
            # Use ffmpeg to mux H.264 stream into MP4 container
            cmd = [
                'ffmpeg',
                '-f', 'h264',
                '-framerate', '24',
                '-i', '-',       # Read from stdin
                '-fflags', '+genpts',  # Generate presentation timestamps
                '-c:v', 'copy',  # Copy video stream (no re-encoding)
                '-f', 'mp4',
                '-movflags', '+faststart',  # Optimize for web playback
                '-y',            # Overwrite if exists
                filename
            ]
            
            self.output = PipeOutput(cmd)
            self.picam2.start_encoder(self.h264_encoder, self.output, name="main")
            
            self.recording_active = True
            self.current_recording_file = filename
            print(f"Started recording to {filename}")
            return {"success": True, "file": filename}
            
        except Exception as e:
            print(f"Start recording failed: {e}")
            self.recording_active = False
            self.h264_encoder = None
            if self.output:
                self.output.close()
                self.output = None
            return {"success": False, "message": str(e)}

    def stop_recording(self):
        if not self.recording_active:
            return {"success": False, "message": "No active recording"}
        
        filename = self.current_recording_file
        try:
            if self.h264_encoder:
                self.picam2.stop_encoder(self.h264_encoder)
                self.h264_encoder = None
            
            if self.output:
                self.output.close()
                self.output = None
            
            self.recording_active = False
            self.current_recording_file = None
            print(f"Stopped recording: {filename}")
            return {"success": True, "file": filename}
            
        except Exception as e:
            print(f"Stop recording failed: {e}")
            self.recording_active = False
            self.h264_encoder = None
            if self.output:
                self.output.close()
                self.output = None
            return {"success": False, "message": str(e)}

    def get_status(self):
        # Check if recording is actually active
        if self.recording_active:
            # Verify encoder and output are still valid
            if self.h264_encoder is None or self.output is None:
                # Cleanup orphaned recording state
                print("Detected orphaned recording state, cleaning up...")
                self.recording_active = False
                self.current_recording_file = None
                if self.output:
                    try:
                        self.output.close()
                    except:
                        pass
                    self.output = None
                self.h264_encoder = None
            elif self.output and hasattr(self.output, 'proc') and self.output.proc.poll() is not None:
                # ffmpeg process has died
                print("Recording process terminated unexpectedly, cleaning up...")
                self.recording_active = False
                self.current_recording_file = None
                self.output = None
                self.h264_encoder = None
        
        return {
            "running": self.running,
            "recording": self.recording_active,
            "file": self.current_recording_file
        }

    def close(self):
        if self.recording_active:
            self.stop_recording()
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
    with open(get_template_path('webrtc_index.html'), 'r') as f:
        content = f.read()
    return web.Response(content_type="text/html", text=content)

async def embed(request):
    with open(get_template_path('webrtc_embed.html'), 'r') as f:
        content = f.read()
    return web.Response(content_type="text/html", text=content)

class PicameraStreamTrack(VideoStreamTrack):
    """
    A custom VideoStreamTrack that captures frames from the global CameraManager.
    """
    def __init__(self):
        super().__init__()
        self.resolution = (960, 540) # Matches 'lores' config (qHD)

    async def recv(self):
        """
        Called by aiortc to get the next frame.
        """
        pts, time_base = await self.next_timestamp()
        
        if not camera_manager or not camera_manager.running:
            # Return a black frame if camera not ready
            frame = av.VideoFrame(width=self.resolution[0], height=self.resolution[1], format='yuv420p')
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = pts
            frame.time_base = time_base
            return frame

        # Capture frame from CameraManager
        loop = asyncio.get_running_loop()
        
        try:
            # Run capture in executor to avoid blocking asyncio loop
            img = await loop.run_in_executor(None, camera_manager.get_frame)
            
            if img is None:
                raise Exception("No frame captured")

            # Create VideoFrame from numpy array
            # picamera2 returns YUV420 as a stacked array (Y + U + V) which matches yuv420p
            frame = av.VideoFrame.from_ndarray(img, format="yuv420p")
            frame.pts = pts
            frame.time_base = time_base
            return frame
            
        except Exception as e:
            # print(f"Error capturing frame: {e}")
            # Return black frame on error
            frame = av.VideoFrame(width=self.resolution[0], height=self.resolution[1], format='yuv420p')
            frame.pts = pts
            frame.time_base = time_base
            return frame

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        print(f"Connection state is {pc.connectionState}")
        if pc.connectionState == "failed":
            await pc.close()
            pcs.discard(pc)
        elif pc.connectionState == "closed":
            pcs.discard(pc)

    # Create and add the video track
    # Note: We create a new track for each connection. 
    # For a real broadcast, we might want to share the capture source.
    try:
        if HAVE_PICAM2:
            # Create track that uses the global camera manager
            video_track = PicameraStreamTrack()
            pc.addTrack(video_track)
        else:
            # Fallback to test pattern or standard device if picam2 missing
            print("Using fallback MediaPlayer (test pattern)")
            # options = {"video_size": "640x480"}
            # player = MediaPlayer("/dev/video0", format="v4l2", options=options)
            # pc.addTrack(player.video)
            pass
            
    except Exception as e:
        print(f"Failed to create video track: {e}")
        return web.Response(status=500, text=str(e))

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })

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

async def reset_recording_state(request):
    """Force reset recording state (emergency cleanup)"""
    try:
        if camera_manager.recording_active:
            camera_manager.stop_recording()
        else:
            # Force cleanup even if not marked as active
            camera_manager.recording_active = False
            camera_manager.current_recording_file = None
            if camera_manager.output:
                try:
                    camera_manager.output.close()
                except:
                    pass
                camera_manager.output = None
            camera_manager.h264_encoder = None
        
        return web.json_response({"success": True, "message": "Recording state reset"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)})

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

    # Close all peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()
    
    # Close camera
    if camera_manager:
        camera_manager.close()

# Global set of peer connections
pcs = set()

def main():
    parser = argparse.ArgumentParser(description="WebRTC Streamer for Pi Camera")
    parser.add_argument("--port", type=int, default=8080, help="Port for web server (default: 8080)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host for web server (default: 0.0.0.0)")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO)

    # Create app with CORS middleware
    app = web.Application(middlewares=[cors_middleware])
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/embed", embed)
    app.router.add_post("/offer", offer)
    
    # Add recording API routes
    app.router.add_post("/start_recording", start_recording)
    app.router.add_post("/stop_recording", stop_recording)
    app.router.add_get("/storage_info", storage_info)
    app.router.add_get("/status", status)
    app.router.add_post("/reset_recording", reset_recording_state)

    print(f"Starting WebRTC server at http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
