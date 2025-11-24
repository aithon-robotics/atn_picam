#!/usr/bin/env python3
"""
Raspberry Pi Camera Stream + Record Server on Raspberry Pi Zero 2W
- Streams camera feed over HTTP as MJPEG (for web viewing)
- Simultaneously records high-quality H.264 to local storage
- Network stream at 5Mbps @ 15fps
- Recording at highest quality settings
Compatible with Pi Camera Module 3 and picamera2
"""

from flask import Flask, Response, render_template
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, MJPEGEncoder
from picamera2.outputs import FileOutput
import io
import threading
import time
import psutil
from datetime import datetime
import os
from atn_picam.core.storage import check_and_cleanup_storage

app = Flask(__name__, template_folder='../templates')

# Global variables
camera = None
h264_encoder = None
mjpeg_output = None
recording_active = False
lock = threading.Lock()
active_clients = 0
bytes_sent = 0
start_time = time.time()
current_recording_file = None

class StreamingOutput(io.BufferedIOBase):
    """Custom output class for streaming MJPEG frames to web clients"""
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()

def init_camera():
    """
    Initialize camera with dual-stream configuration:
    - Main stream: High quality H.264 for recording (hardware encoded)
    - Lores stream: Lower quality MJPEG for web streaming
    """
    global camera, h264_encoder, mjpeg_output, recording_active, current_recording_file
    
    camera = Picamera2()
    
    # Configure for dual operation:
    # - Main: High resolution for H.264 recording (uses full 12MP sensor → downscaled by ISP)
    # - Lores: Lower resolution for MJPEG web streaming
    config = camera.create_video_configuration(
        main={"size": (1920, 1080), "format": "RGB888"},  # High quality for recording
        lores={"size": (1280, 720), "format": "YUV420"},   # Lower res for web stream
        raw={"size": (4608, 2592)},                        # Full sensor capture
        buffer_count=4,  # More buffers for dual encoding
        sensor={"output_size": (4608, 2592), "bit_depth": 10}
    )
    
    camera.configure(config)
    
    # Set camera controls before starting
    camera.set_controls({
        "FrameRate": 30.0,  # Record at 30fps (will be downsampled to 15fps for network)
        "NoiseReductionMode": 0,
        "ScalerCrop": (0, 0, 4608, 2592)  # Use full sensor area
    })
    
    # Start MJPEG encoder for web streaming (uses lores stream)
    mjpeg_output = StreamingOutput()
    mjpeg_encoder = MJPEGEncoder(bitrate=10000000)  # 10Mbps for web viewing
    camera.start_encoder(mjpeg_encoder, FileOutput(mjpeg_output), name="lores")
    
    camera.start()
    
    # Start H.264 recording immediately
    start_recording()

    # CRITICAL: Set ScalerCrop to use the FULL sensor area after recording starts
    # Without this, the ISP may crop/zoom into a portion of the sensor
    # The coordinates are (x, y, width, height) relative to the raw stream size
    time.sleep(0.1)  # Let the camera stabilize
    camera.set_controls({
        "ScalerCrop": (0, 0, 4608, 2592)  # Full sensor area - NO CROPPING
    })
    
    print("\n✓ Camera initialized:")
    print(f"  - Recording: 1920x1080 @ 30fps (H.264, ~15Mbps) → storage")
    print(f"  - Streaming: 1280x720 @ 15fps (MJPEG, ~10Mbps) → web")
    print(f"  - Using full 12MP sensor with hardware ISP downscaling")

def start_recording():
    """Start H.264 recording to file"""
    global h264_encoder, recording_active, current_recording_file
    
    if recording_active:
        print("Recording already active")
        return
    
    # Create recordings directory in home folder
    recordings_dir = os.path.expanduser("~/recordings")
    os.makedirs(recordings_dir, exist_ok=True)
    
    # Check storage and cleanup if necessary
    check_and_cleanup_storage(recordings_dir)
    
    # Generate timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    current_recording_file = os.path.join(recordings_dir, f"drone_{timestamp}.h264")
    
    # Create H.264 encoder for high-quality recording
    h264_encoder = H264Encoder(
        bitrate=15000000,  # 15Mbps - high quality
        repeat=True,       # Repeat SPS/PPS for stream recovery
        iperiod=30,        # I-frame every second (30fps)
        framerate=30       # CRITICAL: Set framerate to match camera (fixes playback speed)
    )
    
    # Create file output for local storage
    file_output = FileOutput(current_recording_file)
    
    # Single H.264 encoder writing directly to file
    # This uses the hardware encoder ONCE for maximum efficiency
    h264_encoder.output = file_output
    
    # Start encoding on the MAIN stream (high resolution)
    camera.start_encoder(h264_encoder, name="main")
    
    recording_active = True
    print(f"\n✓ Started recording to: {current_recording_file}")
    print(f"  - Local file: Full quality (15Mbps)")

def stop_recording():
    """Stop H.264 recording"""
    global recording_active, h264_encoder
    
    if not recording_active:
        print("No active recording to stop")
        return
    
    if h264_encoder:
        camera.stop_encoder(h264_encoder)
        h264_encoder = None
    
    recording_active = False
    print(f"\n✓ Stopped recording: {current_recording_file}")

def monitor_network():
    """Monitor and print network statistics every 5 seconds"""
    global bytes_sent, active_clients, start_time, current_recording_file
    
    last_bytes_sent = 0
    
    while True:
        time.sleep(5)
        
        # Calculate bandwidth
        current_bytes = bytes_sent
        interval_bytes = current_bytes - last_bytes_sent
        last_bytes_sent = current_bytes
        
        # Convert to Mbps
        mbps = (interval_bytes * 8) / (5 * 1024 * 1024)
        total_mb = current_bytes / (1024 * 1024)
        
        # CPU and memory
        cpu_percent = psutil.cpu_percent(interval=0)
        mem = psutil.virtual_memory()
        
        # Recording status and file size
        rec_status = "RECORDING" if recording_active else "IDLE"
        file_size_mb = 0
        if recording_active and current_recording_file and os.path.exists(current_recording_file):
            file_size_mb = os.path.getsize(current_recording_file) / (1024 * 1024)
        
        print(f"[Stats] {rec_status} | Web clients: {active_clients} | "
              f"Stream: {mbps:.2f} Mbps | Total: {total_mb:.1f} MB | "
              f"Recording: {file_size_mb:.1f} MB | "
              f"CPU: {cpu_percent}% | RAM: {mem.percent}%")

def generate_frames():
    """
    Generator that yields MJPEG frames for web streaming.
    This runs continuously, waiting for new frames from the camera.
    """
    global mjpeg_output, active_clients, bytes_sent
    
    active_clients += 1
    print(f"[Network] Web client connected. Active clients: {active_clients}")
    
    if mjpeg_output is None:
        active_clients -= 1
        return
    
    try:
        while True:
            # Wait for a new frame from the camera
            with mjpeg_output.condition:
                mjpeg_output.condition.wait()
                frame = mjpeg_output.frame
            
            # Track bytes sent
            frame_data = (b'--FRAME\r\n'
                         b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            bytes_sent += len(frame_data)
            
            # Yield frame in multipart format for MJPEG streaming
            yield frame_data
    finally:
        active_clients -= 1
        print(f"[Network] Web client disconnected. Active clients: {active_clients}")

@app.route('/')
def index():
    """Serve the main web page with embedded camera stream"""
    return render_template('pizero_recording.html')

@app.route('/stream')
def video_feed():
    """Provide the MJPEG stream endpoint"""
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=FRAME')

@app.route('/status')
def status():
    """API endpoint to check camera status"""
    if camera is not None:
        return {
            'status': 'running',
            'recording': recording_active,
            'file': current_recording_file,
            'resolution': '1920x1080 @ 30fps',
            'web_stream': '1280x720 @ 15fps'
        }, 200
    return {'status': 'stopped'}, 503

def main():
    try:
        print("Initializing camera system...")
        print("=" * 60)
        init_camera()
        
        # Start network monitoring thread
        monitor_thread = threading.Thread(target=monitor_network, daemon=True)
        monitor_thread.start()
        
        print("\n" + "=" * 60)
        print("Camera system ready!")
        print("=" * 60)
        print(f"Web interface: http://<your-pi-ip>:8080")
        print(f"Recording to: {current_recording_file}")
        print("Press Ctrl+C to stop")
        print("=" * 60 + "\n")
        
        # Run Flask server
        app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
        
    except KeyboardInterrupt:
        print("\n\nStopping camera system...")
        stop_recording()
    finally:
        if camera:
            camera.stop()
            camera.close()
        print("Camera closed. Goodbye!")

if __name__ == '__main__':
    main()