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
from datetime import datetime

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaPlayer
import av

# Try importing Picamera2
try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FileOutput
    HAVE_PICAM2 = True
except ImportError:
    HAVE_PICAM2 = False
    print("Warning: picamera2 not found. Will try to use standard video device if available.")

# Storage management constants
MIN_FREE_SPACE_GB = 10
MIN_FREE_SPACE_BYTES = MIN_FREE_SPACE_GB * 1024 * 1024 * 1024

def check_and_cleanup_storage(recordings_dir):
    """
    Check available storage space and delete oldest recordings if below threshold.
    Maintains at least 10GB of free space.
    """
    try:
        # Get disk usage statistics
        stat = os.statvfs(recordings_dir)
        free_space_bytes = stat.f_bavail * stat.f_frsize
        free_space_gb = free_space_bytes / (1024 * 1024 * 1024)
        
        if free_space_bytes >= MIN_FREE_SPACE_BYTES:
            return
        
        print(f"WARNING: Low storage space ({free_space_gb:.2f} GB). Cleaning up...")
        
        # Get all recording files sorted by modification time (oldest first)
        recordings = []
        for filename in os.listdir(recordings_dir):
            filepath = os.path.join(recordings_dir, filename)
            if os.path.isfile(filepath) and (filename.endswith('.mp4') or filename.endswith('.h264')):
                recordings.append((filepath, os.path.getmtime(filepath), os.path.getsize(filepath)))
        
        recordings.sort(key=lambda x: x[1])
        
        for filepath, mtime, size in recordings:
            if free_space_bytes >= MIN_FREE_SPACE_BYTES:
                break
            try:
                os.remove(filepath)
                free_space_bytes += size
                print(f"Deleted: {os.path.basename(filepath)}")
            except OSError as e:
                print(f"Failed to delete {filepath}: {e}")
                
    except Exception as e:
        print(f"Storage cleanup error: {e}")

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
            filename = os.path.join(recordings_dir, f"webrtc_{timestamp}.h264")
            
            # Create H.264 encoder attached to the MAIN (high-res) stream
            self.h264_encoder = H264Encoder(
                bitrate=20000000, # 20 Mbps (Higher quality)
                repeat=True,
                iperiod=24, # Match framerate
                framerate=24
            )
            
            self.picam2.start_encoder(self.h264_encoder, FileOutput(filename), name="main")
            
            self.recording_active = True
            self.current_recording_file = filename
            print(f"Started recording to {filename}")
            return {"success": True, "file": filename}
            
        except Exception as e:
            print(f"Start recording failed: {e}")
            self.recording_active = False
            self.h264_encoder = None
            return {"success": False, "message": str(e)}

    def stop_recording(self):
        if not self.recording_active:
            return {"success": False, "message": "No active recording"}
        
        filename = self.current_recording_file
        try:
            if self.h264_encoder:
                self.picam2.stop_encoder(self.h264_encoder)
                self.h264_encoder = None
            
            self.recording_active = False
            self.current_recording_file = None
            print(f"Stopped recording: {filename}")
            return {"success": True, "file": filename}
            
        except Exception as e:
            print(f"Stop recording failed: {e}")
            self.recording_active = False
            self.h264_encoder = None
            return {"success": False, "message": str(e)}

    def get_status(self):
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

# HTML content for the client
INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>PiCam WebRTC Stream + Record</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; text-align: center; background: #f0f2f5; margin: 0; padding: 20px; }
        .container { margin: 0 auto; padding: 20px; background: white; max-width: 1000px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #1a1a1a; margin-bottom: 20px; }
        video { width: 100%; max-width: 960px; background: #000; border-radius: 8px; aspect-ratio: 16/9; }
        .controls { margin-top: 20px; display: flex; justify-content: center; gap: 10px; flex-wrap: wrap; }
        button { padding: 12px 24px; font-size: 16px; font-weight: 600; cursor: pointer; border: none; border-radius: 6px; transition: background 0.2s; color: white; }
        button:disabled { background: #ccc !important; cursor: not-allowed; }
        
        .btn-stream { background: #007bff; }
        .btn-stream:hover { background: #0056b3; }
        .btn-stop { background: #6c757d; }
        
        .btn-record { background: #dc3545; }
        .btn-record:hover { background: #c82333; }
        .btn-record.recording { animation: pulse 1.5s infinite; }
        
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        
        #status { margin-top: 15px; color: #666; font-size: 14px; }
        .stats { margin-top: 10px; font-size: 12px; color: #888; font-family: monospace; }
        .storage-info { margin-top: 10px; font-size: 13px; color: #555; background: #e9ecef; padding: 8px; border-radius: 4px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>PiCam WebRTC Stream</h1>
        <video id="video" autoplay playsinline muted></video>
        
        <div class="controls">
            <button id="startBtn" class="btn-stream" onclick="start()">Start Stream</button>
            <button id="stopBtn" class="btn-stop" onclick="stop()" disabled>Stop Stream</button>
            <button id="recordBtn" class="btn-record" onclick="toggleRecording()">Start Recording</button>
        </div>
        
        <div id="status">Ready to connect</div>
        <div id="storageInfo" class="storage-info">Loading storage info...</div>
        <div id="stats" class="stats"></div>
    </div>

    <script>
        const video = document.getElementById('video');
        const status = document.getElementById('status');
        const startBtn = document.getElementById('startBtn');
        const stopBtn = document.getElementById('stopBtn');
        const recordBtn = document.getElementById('recordBtn');
        const storageDiv = document.getElementById('storageInfo');
        const statsDiv = document.getElementById('stats');
        
        let pc = null;
        let statsInterval = null;
        let isRecording = false;

        async function start() {
            startBtn.disabled = true;
            stopBtn.disabled = false;
            status.textContent = "Connecting...";
            
            const config = {
                iceServers: [{ urls: ['stun:stun.l.google.com:19302'] }]
            };
            
            pc = new RTCPeerConnection(config);
            
            pc.ontrack = (evt) => {
                status.textContent = "Stream received";
                if (video.srcObject !== evt.streams[0]) {
                    video.srcObject = evt.streams[0];
                }
            };
            
            pc.oniceconnectionstatechange = () => {
                status.textContent = "Connection: " + pc.iceConnectionState;
                if (pc.iceConnectionState === 'disconnected' || pc.iceConnectionState === 'failed') {
                    stop();
                }
            };

            pc.addTransceiver('video', {direction: 'recvonly'});
            
            try {
                const offer = await pc.createOffer();
                await pc.setLocalDescription(offer);
                
                const response = await fetch('/offer', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        sdp: pc.localDescription.sdp,
                        type: pc.localDescription.type
                    })
                });
                
                const answer = await response.json();
                await pc.setRemoteDescription(answer);
                
                startStats();
                
            } catch (e) {
                status.textContent = "Error: " + e;
                console.error(e);
                stop();
            }
        }

        function stop() {
            if (pc) {
                pc.close();
                pc = null;
            }
            if (statsInterval) {
                clearInterval(statsInterval);
                statsInterval = null;
            }
            video.srcObject = null;
            startBtn.disabled = false;
            stopBtn.disabled = true;
            status.textContent = "Stopped";
            statsDiv.textContent = "";
        }
        
        function startStats() {
            statsInterval = setInterval(async () => {
                if (!pc) return;
                const stats = await pc.getStats();
                let statsText = "";
                stats.forEach(report => {
                    if (report.type === 'inbound-rtp' && report.kind === 'video') {
                        if (report.frameWidth) {
                            statsText = `${report.frameWidth}x${report.frameHeight} | Frames: ${report.framesDecoded}`;
                        }
                    }
                });
                if (statsText) statsDiv.textContent = statsText;
            }, 1000);
        }

        async function toggleRecording() {
            recordBtn.disabled = true;
            const endpoint = isRecording ? '/stop_recording' : '/start_recording';
            
            try {
                const response = await fetch(endpoint, { method: 'POST' });
                const data = await response.json();
                
                if (data.success) {
                    isRecording = !isRecording;
                    updateRecordUI();
                    updateStorageInfo();
                } else {
                    alert("Error: " + data.message);
                }
            } catch (e) {
                alert("Request failed: " + e);
            } finally {
                recordBtn.disabled = false;
            }
        }

        function updateRecordUI() {
            if (isRecording) {
                recordBtn.textContent = "Stop Recording";
                recordBtn.classList.add("recording");
            } else {
                recordBtn.textContent = "Start Recording";
                recordBtn.classList.remove("recording");
            }
        }

        async function updateStorageInfo() {
            try {
                const response = await fetch('/storage_info');
                const data = await response.json();
                if (data.success) {
                    storageDiv.textContent = `Storage: ${data.free_gb} GB free / ${data.total_gb} GB total`;
                }
            } catch (e) {
                console.error(e);
            }
        }

        // Initial checks
        updateStorageInfo();
        setInterval(updateStorageInfo, 10000);
        
        // Check status on load
        fetch('/status').then(r => r.json()).then(data => {
            isRecording = data.recording;
            updateRecordUI();
        });
    </script>
</body>
</html>
"""

# Minimal HTML for embedding in iframes
EMBED_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>PiCam Stream</title>
    <style>
        body, html { margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; background: #000; }
        video { width: 100%; height: 100%; object-fit: contain; }
    </style>
</head>
<body>
    <video id="video" autoplay playsinline muted></video>
    <script>
        const video = document.getElementById('video');
        const config = { iceServers: [{ urls: ['stun:stun.l.google.com:19302'] }] };
        
        async function start() {
            const pc = new RTCPeerConnection(config);
            
            pc.ontrack = (evt) => {
                if (video.srcObject !== evt.streams[0]) {
                    video.srcObject = evt.streams[0];
                }
            };

            // Add transceiver to receive video only
            pc.addTransceiver('video', {direction: 'recvonly'});
            
            try {
                const offer = await pc.createOffer();
                await pc.setLocalDescription(offer);
                
                const response = await fetch('/offer', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        sdp: pc.localDescription.sdp,
                        type: pc.localDescription.type
                    })
                });
                
                const answer = await response.json();
                await pc.setRemoteDescription(answer);
            } catch (e) {
                console.error("Connection failed", e);
                // Retry after 3 seconds on failure
                setTimeout(start, 3000);
            }
        }
        
        start();
    </script>
</body>
</html>
"""

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

async def index(request):
    return web.Response(content_type="text/html", text=INDEX_HTML)

async def embed(request):
    return web.Response(content_type="text/html", text=EMBED_HTML)

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

if __name__ == "__main__":
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
    app.router.add_post("/offer", offer)
    
    # Add recording API routes
    app.router.add_post("/start_recording", start_recording)
    app.router.add_post("/stop_recording", stop_recording)
    app.router.add_get("/storage_info", storage_info)
    app.router.add_get("/status", status)

    print(f"Starting WebRTC server at http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)
