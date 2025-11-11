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

app = Flask(__name__)

# Global variables
camera = None
output = None
lock = threading.Lock()
active_clients = 0
bytes_sent = 0
start_time = time.time()

class StreamingOutput(io.BufferedIOBase):
    """Custom output class for streaming frames"""
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()

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

    
    print("\n✓ Camera streaming full 12MP sensor → ISP downscaled to 1280x720 @ 15fps")

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
