#!/usr/bin/env python3
"""
Raspberry Pi Camera Stream Server
Streams camera feed over HTTP as MJPEG
Compatible with Pi Camera Module 3 and picamera2
"""

from flask import Flask, Response, render_template_string
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
import io
import threading
import time
import psutil
import cv2
import numpy as np
from collections import deque

app = Flask(__name__)

# Global variables
camera = None
output = None
lock = threading.Lock()
active_clients = 0
bytes_sent = 0
start_time = time.time()

# Video stabilization variables
ENABLE_STABILIZATION = True
SMOOTHING_RADIUS = 15  # Lower = more responsive, higher = smoother (was 30)
STABILIZATION_STRENGTH = 0.3  # 0.0 to 1.0, lower = less aggressive
CROP_PERCENT = 5  # Crop 5% from edges to hide border artifacts
prev_gray = None
transforms_buffer = deque(maxlen=SMOOTHING_RADIUS)
stabilized_frame = None

class StreamingOutput(io.BufferedIOBase):
    """Custom output class for streaming frames"""
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()

def stabilize_frame(frame_jpeg):
    """
    Apply video stabilization using optical flow and motion smoothing.
    Reduces vibrations by tracking motion between frames.
    """
    global prev_gray, transforms_buffer, ENABLE_STABILIZATION
    
    if not ENABLE_STABILIZATION:
        return frame_jpeg
    
    try:
        # Decode JPEG to numpy array
        frame = cv2.imdecode(np.frombuffer(frame_jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return frame_jpeg
        
        h, w = frame.shape[:2]
        
        # Convert to grayscale for motion detection
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        # Initialize on first frame
        if prev_gray is None:
            prev_gray = gray
            return frame_jpeg
        
        # Detect feature points in previous frame (fewer points = faster)
        prev_pts = cv2.goodFeaturesToTrack(prev_gray, maxCorners=100, qualityLevel=0.01,
                                           minDistance=30, blockSize=3)
        
        if prev_pts is None:
            prev_gray = gray
            return frame_jpeg
        
        # Calculate optical flow (track points from previous to current frame)
        curr_pts, status, err = cv2.calcOpticalFlowPyrLK(prev_gray, gray, prev_pts, None)
        
        # Filter only valid points
        idx = np.where(status == 1)[0]
        prev_pts = prev_pts[idx]
        curr_pts = curr_pts[idx]
        
        if len(prev_pts) < 10:  # Need at least 10 points for reliable estimation
            prev_gray = gray
            return frame_jpeg
        
        # Estimate rigid transformation (translation + rotation only, no scale)
        transform = cv2.estimateAffinePartial2D(prev_pts, curr_pts)[0]
        
        if transform is None:
            prev_gray = gray
            return frame_jpeg
        
        # Extract transformation parameters
        dx = transform[0, 2]  # Translation X
        dy = transform[1, 2]  # Translation Y
        da = np.arctan2(transform[1, 0], transform[0, 0])  # Rotation angle
        
        # Store transformation
        transforms_buffer.append([dx, dy, da])
        
        # Calculate smoothed transformation (moving average)
        if len(transforms_buffer) >= 3:  # Need at least 3 frames
            transforms_array = np.array(transforms_buffer)
            
            # Use weighted moving average (more recent frames have higher weight)
            weights = np.linspace(0.5, 1.0, len(transforms_array))
            weights = weights / weights.sum()
            smoothed = np.average(transforms_array, axis=0, weights=weights)
            
            # Calculate correction (difference between current and smoothed)
            diff_dx = (smoothed[0] - dx) * STABILIZATION_STRENGTH
            diff_dy = (smoothed[1] - dy) * STABILIZATION_STRENGTH
            diff_da = (smoothed[2] - da) * STABILIZATION_STRENGTH
            
            # Limit maximum correction to prevent over-correction
            max_correction = 10  # pixels
            diff_dx = np.clip(diff_dx, -max_correction, max_correction)
            diff_dy = np.clip(diff_dy, -max_correction, max_correction)
            diff_da = np.clip(diff_da, -0.05, 0.05)  # radians (~3 degrees max)
            
            # Create stabilization transform matrix
            stabilize_transform = np.array([
                [np.cos(diff_da), -np.sin(diff_da), diff_dx],
                [np.sin(diff_da), np.cos(diff_da), diff_dy]
            ], dtype=np.float32)
            
            # Apply stabilization
            stabilized = cv2.warpAffine(frame, stabilize_transform, (w, h),
                                       borderMode=cv2.BORDER_REPLICATE)
            
            # Crop edges to remove border artifacts
            crop_x = int(w * CROP_PERCENT / 100)
            crop_y = int(h * CROP_PERCENT / 100)
            stabilized_cropped = stabilized[crop_y:h-crop_y, crop_x:w-crop_x]
            
            # Resize back to original dimensions
            stabilized_final = cv2.resize(stabilized_cropped, (w, h))
            
            # Encode back to JPEG
            _, buffer = cv2.imencode('.jpg', stabilized_final, [cv2.IMWRITE_JPEG_QUALITY, 85])
            prev_gray = gray
            return buffer.tobytes()
        
        prev_gray = gray
        return frame_jpeg
        
    except Exception as e:
        print(f"[Stabilization] Error: {e}")
        return frame_jpeg

def init_camera():
    """Initialize the camera with full 12MP sensor access"""
    global camera, output
    
    camera = Picamera2()
    
    # Configure for FULL 12MP sensor capture (4608x2592)
    # The ISP (Image Signal Processor) automatically downscales raw → main
    # This gives you the full field of view with hardware downscaling
    config = camera.create_video_configuration(
        main={"size": (1280, 720), "format": "RGB888"},  # Output size (after ISP downscaling)
        raw={"size": (4608, 2592)},                      # Input size (from sensor)
        encode="main",
        buffer_count=2,
        sensor={"output_size": (4608, 2592), "bit_depth": 10}  # Use full resolution sensor mode
    )
    
    camera.configure(config)
    
    output = StreamingOutput()
    
    # Set controls including ScalerCrop BEFORE starting recording for faster startup
    camera.set_controls({
        "FrameRate": 15.0,
        "NoiseReductionMode": 0,
        "ScalerCrop": (0, 0, 4608, 2592)  # Full sensor area - NO CROPPING
    })
    
    camera.start_recording(MJPEGEncoder(bitrate=10000000), FileOutput(output))
    
    # CRITICAL: Set ScalerCrop to use the FULL sensor area after recording starts
    # Without this, the ISP may crop/zoom into a portion of the sensor
    # The coordinates are (x, y, width, height) relative to the raw stream size
    time.sleep(0.1)  # Let the camera stabilize
    camera.set_controls({
        "ScalerCrop": (0, 0, 4608, 2592)  # Full sensor area - NO CROPPING
    })

    
    print("\n✓ Camera streaming: 12MP sensor → ISP downscaled to 1280x720 @ 15fps")
    if ENABLE_STABILIZATION:
        print("  Video stabilization: ENABLED (OpenCV optical flow)")
    else:
        print("  Video stabilization: DISABLED")

def monitor_network():
    """Monitor and print network statistics every 5 seconds"""
    global bytes_sent, active_clients, start_time
    
    net_io_start = psutil.net_io_counters()
    last_bytes_sent = 0
    
    while True:
        time.sleep(5)
        
        # Calculate bandwidth
        elapsed = time.time() - start_time
        current_bytes = bytes_sent
        interval_bytes = current_bytes - last_bytes_sent
        last_bytes_sent = current_bytes
        
        # Convert to Mbps
        mbps = (interval_bytes * 8) / (5 * 1024 * 1024)
        total_mb = current_bytes / (1024 * 1024)
        
        # Get system network stats
        net_io = psutil.net_io_counters()
        system_sent_mb = net_io.bytes_sent / (1024 * 1024)
        
        # CPU and memory
        cpu_percent = psutil.cpu_percent(interval=0)
        mem = psutil.virtual_memory()
        
        print(f"[Stats] Clients: {active_clients} | "
              f"Stream: {mbps:.2f} Mbps | "
              f"Total: {total_mb:.1f} MB | "
              f"CPU: {cpu_percent}% | "
              f"RAM: {mem.percent}%")


def generate_frames():
    """
    Generator that yields MJPEG frames for streaming.
    This runs continuously, waiting for new frames from the camera.
    """
    global output, active_clients, bytes_sent
    
    active_clients += 1
    print(f"[Network] Client connected. Active clients: {active_clients}")
    
    if output is None:
        active_clients -= 1
        return
    
    try:
        while True:
            # Wait for a new frame from the camera
            with output.condition:
                output.condition.wait()
                frame = output.frame
            
            # Apply video stabilization
            if ENABLE_STABILIZATION:
                frame = stabilize_frame(frame)
            
            # Track bytes sent
            frame_data = (b'--FRAME\r\n'
                         b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            bytes_sent += len(frame_data)
            
            # Yield frame in multipart format for MJPEG streaming
            yield frame_data
    finally:
        active_clients -= 1
        print(f"[Network] Client disconnected. Active clients: {active_clients}")

@app.route('/')
def index():
    """Serve the main web page with embedded camera stream"""
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Pi Camera Stream</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    text-align: center;
                    background-color: #f0f0f0;
                    margin: 0;
                    padding: 20px;
                }
                h1 {
                    color: #333;
                }
                .stream-container {
                    margin: 20px auto;
                    max-width: 1280px;
                    background: white;
                    padding: 20px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }
                img {
                    max-width: 100%;
                    height: auto;
                    border: 2px solid #333;
                    border-radius: 5px;
                }
                .info {
                    margin-top: 20px;
                    color: #666;
                    font-size: 14px;
                }
            </style>
        </head>
        <body>
            <h1>🎥 Raspberry Pi Camera Stream</h1>
            <div class="stream-container">
                <h2>Live Feed</h2>
                <img src="{{ url_for('video_feed') }}" alt="Camera Stream">
                <div class="info">
                    <p>Stream URL: <code>http://{{ request.host }}/stream</code></p>
                    <p>Refresh the page if the stream doesn't load immediately.</p>
                </div>
            </div>
        </body>
        </html>
    ''')

@app.route('/stream')
def video_feed():
    """Provide the MJPEG stream endpoint"""
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=FRAME')

@app.route('/status')
def status():
    """API endpoint to check camera status"""
    if camera is not None:
        return {'status': 'running', 'resolution': '1280x720'}, 200
    return {'status': 'stopped'}, 503

if __name__ == '__main__':
    try:
        print("Initializing camera...")
        init_camera()
        
        # Start network monitoring thread
        monitor_thread = threading.Thread(target=monitor_network, daemon=True)
        monitor_thread.start()
        
        print("Starting web server...")
        print("Access the stream at: http://<your-pi-ip>:8080")
        print("Press Ctrl+C to stop")
        
        # Run Flask server
        # host='0.0.0.0' makes it accessible from other devices on the network
        app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
        
    except KeyboardInterrupt:
        print("\nStopping camera stream...")
    finally:
        if camera:
            camera.stop_recording()
            camera.close()
        print("Camera closed. Goodbye!")
