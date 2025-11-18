#!/usr/bin/env python3
"""
Jetson Orin Nano Camera Stream + Record Server
- Streams camera feed over HTTP as MJPEG (for web viewing)
- Simultaneously records high-quality H.264 to local storage
- Uses Pi Camera Module v2 via CSI interface
- Optimized with NVIDIA hardware acceleration (NVENC + NVJPEG)

Hardware acceleration:
- nvv4l2camerasrc: Camera capture with ISP
- nvv4l2h264enc: Hardware H.264 encoding (NVENC)
- nvjpegenc: Hardware JPEG encoding
- NVMM: Zero-copy memory management
"""

from flask import Flask, Response, render_template_string
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import threading
import time
import psutil
from datetime import datetime
import os
import queue

# Initialize GStreamer
Gst.init(None)

app = Flask(__name__)

# Storage management constants
MIN_FREE_SPACE_GB = 10
MIN_FREE_SPACE_BYTES = MIN_FREE_SPACE_GB * 1024 * 1024 * 1024

def check_and_cleanup_storage(recordings_dir):
    """
    Check available storage space and delete oldest recordings if below threshold.
    Maintains at least 10GB of free space.
    """
    # Get disk usage statistics
    stat = os.statvfs(recordings_dir)
    free_space_bytes = stat.f_bavail * stat.f_frsize
    free_space_gb = free_space_bytes / (1024 * 1024 * 1024)
    
    if free_space_bytes >= MIN_FREE_SPACE_BYTES:
        print(f"[Storage] Available space: {free_space_gb:.2f} GB (OK)")
        return
    
    print(f"\n⚠️  WARNING: Low storage space detected!")
    print(f"[Storage] Available: {free_space_gb:.2f} GB (threshold: {MIN_FREE_SPACE_GB} GB)")
    print(f"[Storage] Starting cleanup of oldest recordings...")
    
    # Get all recording files sorted by modification time (oldest first)
    recordings = []
    for filename in os.listdir(recordings_dir):
        filepath = os.path.join(recordings_dir, filename)
        if os.path.isfile(filepath) and (filename.endswith('.mp4') or filename.endswith('.h264')):
            recordings.append((filepath, os.path.getmtime(filepath), os.path.getsize(filepath)))
    
    recordings.sort(key=lambda x: x[1])  # Sort by modification time
    
    # Delete oldest files until we have enough space
    deleted_count = 0
    deleted_size_mb = 0
    
    for filepath, mtime, size in recordings:
        if free_space_bytes >= MIN_FREE_SPACE_BYTES:
            break
        
        try:
            os.remove(filepath)
            deleted_count += 1
            deleted_size_mb += size / (1024 * 1024)
            free_space_bytes += size
            print(f"[Storage] Deleted: {os.path.basename(filepath)} ({size/(1024*1024):.1f} MB)")
        except OSError as e:
            print(f"[Storage] Failed to delete {filepath}: {e}")
    
    free_space_gb = free_space_bytes / (1024 * 1024 * 1024)
    print(f"[Storage] Cleanup complete: Deleted {deleted_count} file(s) ({deleted_size_mb:.1f} MB)")
    print(f"[Storage] Available space now: {free_space_gb:.2f} GB\n")

# Global variables
pipeline = None
h264_filesink = None
recording_active = False
active_clients = 0
bytes_sent = 0
start_time = time.time()
current_recording_file = None
frame_queue = queue.Queue(maxsize=2)
pipeline_thread = None
main_loop = None

class GStreamerPipeline:
    """
    Manages dual GStreamer pipeline:
    - Stream 1: Camera → H.264 encoder → File
    - Stream 2: Camera → JPEG encoder → App (for web streaming)
    
    Uses tee element to split single camera source into two independent pipelines.
    """
    
    def __init__(self):
        self.pipeline = None
        self.appsink = None
        self.filesink = None
        self.loop = None
        
    def create_pipeline(self, recording_file):
        """
        Create GStreamer pipeline with dual output:
        1. High-quality H.264 recording to file
        2. MJPEG stream for web viewing
        
        Pipeline structure:
        nvv4l2camerasrc → tee → [queue → nvvidconv → nvv4l2h264enc → filesink]
                              → [queue → nvvidconv → nvjpegenc → appsink]
        """
        
        # Build pipeline string
        # Note: Pi Camera Module v2 max resolution is 3280x2464 (8MP)
        # Using nvarguscamerasrc for CSI camera interface (not nvv4l2camerasrc)
        pipeline_str = (
            # Camera source with ARGUS (for CSI cameras like Pi Camera Module)
            "nvarguscamerasrc sensor-id=0 ! "
            "video/x-raw(memory:NVMM), width=1920, height=1080, framerate=30/1, format=NV12 ! "
            
            # Rotate 180° to fix upside-down image (flip-method=2)
            # flip-method values: 0=none, 1=counterclockwise, 2=rotate-180, 3=clockwise, 4=horizontal-flip, 5=vertical-flip
            "nvvidconv flip-method=2 ! "
            "video/x-raw(memory:NVMM), format=NV12 ! "
            
            # Tee to split into two streams
            "tee name=t "
            
            # Branch 1: H.264 recording to file
            "t. ! queue max-size-buffers=2 leaky=downstream ! "
            "nvv4l2h264enc bitrate=15000000 iframeinterval=30 insert-sps-pps=true insert-vui=true ! "
            "h264parse ! "
            "qtmux ! "
            f"filesink location={recording_file} name=filesink sync=false "
            
            # Branch 2: JPEG encoding for web stream
            "t. ! queue max-size-buffers=2 leaky=downstream ! "
            "nvvidconv ! "
            "video/x-raw, format=I420 ! "
            "videoscale ! "
            "video/x-raw, width=1280, height=720 ! "
            "videorate ! "
            "video/x-raw, framerate=15/1 ! "
            "nvjpegenc quality=80 ! "
            "appsink name=appsink emit-signals=true max-buffers=2 drop=true"
        )
        
        print(f"\n[GStreamer] Creating pipeline...")
        print(f"[GStreamer] Recording: 1920x1080 @ 30fps H.264 → {recording_file}")
        print(f"[GStreamer] Streaming: 1280x720 @ 15fps MJPEG → web")
        
        # Parse and create pipeline
        self.pipeline = Gst.parse_launch(pipeline_str)
        
        # Get appsink element for JPEG frames
        self.appsink = self.pipeline.get_by_name("appsink")
        self.appsink.connect("new-sample", self.on_new_sample)
        
        # Get filesink element
        self.filesink = self.pipeline.get_by_name("filesink")
        
        # Set up bus for error handling
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus_message)
        
        return self.pipeline
    
    def on_new_sample(self, appsink):
        """
        Callback when new JPEG frame is available.
        Pulls sample from appsink and adds to queue for Flask streaming.
        """
        sample = appsink.emit("pull-sample")
        if sample:
            buffer = sample.get_buffer()
            # Extract JPEG data
            success, map_info = buffer.map(Gst.MapFlags.READ)
            if success:
                jpeg_data = map_info.data
                # Add to queue (non-blocking, drop if full)
                try:
                    frame_queue.put_nowait(bytes(jpeg_data))
                except queue.Full:
                    pass  # Drop frame if queue is full
                buffer.unmap(map_info)
        
        return Gst.FlowReturn.OK
    
    def on_bus_message(self, bus, message):
        """Handle GStreamer bus messages"""
        t = message.type
        
        if t == Gst.MessageType.EOS:
            print("[GStreamer] End of stream")
            self.stop()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"[GStreamer] Error: {err}, {debug}")
            self.stop()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print(f"[GStreamer] Warning: {warn}, {debug}")
        
        return True
    
    def start(self):
        """Start the GStreamer pipeline"""
        if self.pipeline:
            print("[GStreamer] Starting pipeline...")
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                print("[GStreamer] Failed to start pipeline")
                return False
            print("[GStreamer] Pipeline started successfully")
            return True
        return False
    
    def stop(self):
        """Stop the GStreamer pipeline cleanly with EOS"""
        if self.pipeline:
            print("[GStreamer] Sending EOS (End of Stream) to finalize recording...")
            
            # Send EOS event to properly finalize the MP4 file
            self.pipeline.send_event(Gst.Event.new_eos())
            
            # Wait for EOS to be processed (max 5 seconds)
            bus = self.pipeline.get_bus()
            msg = bus.timed_pop_filtered(
                5 * Gst.SECOND,
                Gst.MessageType.EOS | Gst.MessageType.ERROR
            )
            
            if msg:
                if msg.type == Gst.MessageType.EOS:
                    print("[GStreamer] EOS received, file finalized successfully")
                elif msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    print(f"[GStreamer] Error during EOS: {err}")
            else:
                print("[GStreamer] Warning: EOS timeout (file may be incomplete)")
            
            # Now stop the pipeline
            print("[GStreamer] Stopping pipeline...")
            self.pipeline.set_state(Gst.State.PAUSED)
            time.sleep(0.1)
            self.pipeline.set_state(Gst.State.READY)
            time.sleep(0.1)
            self.pipeline.set_state(Gst.State.NULL)
            
            # Wait for state change to complete
            ret = self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
            if ret[0] == Gst.StateChangeReturn.SUCCESS:
                print("[GStreamer] Pipeline stopped cleanly")
            else:
                print("[GStreamer] Warning: Pipeline state change incomplete")
            
            if self.loop:
                self.loop.quit()
    
    def run_loop(self):
        """Run GLib main loop (required for GStreamer)"""
        self.loop = GLib.MainLoop()
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass

def init_camera():
    """
    Initialize camera system with GStreamer pipeline.
    Creates dual-stream output:
    - H.264 recording to file (hardware encoded)
    - MJPEG stream for web (hardware encoded)
    """
    global pipeline, recording_active, current_recording_file, pipeline_thread, main_loop
    
    # Create recordings directory in home folder
    recordings_dir = os.path.expanduser("~/recordings")
    os.makedirs(recordings_dir, exist_ok=True)
    
    # Check storage and cleanup if necessary
    check_and_cleanup_storage(recordings_dir)
    
    # Generate timestamped filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    current_recording_file = os.path.join(recordings_dir, f"jetson_{timestamp}.mp4")
    
    # Create GStreamer pipeline
    pipeline = GStreamerPipeline()
    pipeline.create_pipeline(current_recording_file)
    
    # Start pipeline
    if pipeline.start():
        recording_active = True
        
        # Run GLib main loop in separate thread
        pipeline_thread = threading.Thread(target=pipeline.run_loop, daemon=True)
        pipeline_thread.start()
        
        print("\n✓ Camera initialized:")
        print(f"  - Recording: 1920x1080 @ 30fps (H.264, 15Mbps) → {current_recording_file}")
        print(f"  - Streaming: 1280x720 @ 15fps (MJPEG, ~10Mbps) → web")
        print(f"  - Hardware: NVENC (H.264) + NVJPEG (JPEG) + NVMM (zero-copy)")
        print(f"  - Sensor: Pi Camera Module v2 (8MP, 3280×2464)")
        
        return True
    else:
        print("✗ Failed to initialize camera")
        return False

def stop_recording():
    """Stop recording and clean up pipeline"""
    global recording_active, pipeline, pipeline_thread
    
    if pipeline and recording_active:
        recording_active = False
        pipeline.stop()
        
        # Wait for pipeline thread to finish
        if pipeline_thread and pipeline_thread.is_alive():
            print("[GStreamer] Waiting for pipeline thread to finish...")
            pipeline_thread.join(timeout=3.0)
        
        print(f"\n✓ Stopped recording: {current_recording_file}")
        
        # Give system time to release camera resources
        time.sleep(0.5)

def monitor_stats():
    """Monitor and print system statistics every 5 seconds"""
    global bytes_sent, active_clients, current_recording_file
    
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
        
        # Queue status
        queue_size = frame_queue.qsize()
        
        print(f"[Stats] {rec_status} | Web clients: {active_clients} | "
              f"Stream: {mbps:.2f} Mbps | Total: {total_mb:.1f} MB | "
              f"Recording: {file_size_mb:.1f} MB | "
              f"Queue: {queue_size} | "
              f"CPU: {cpu_percent:.1f}% | RAM: {mem.percent:.1f}%")

def generate_frames():
    """
    Generator that yields MJPEG frames for web streaming.
    Pulls JPEG frames from the GStreamer appsink queue.
    """
    global active_clients, bytes_sent
    
    active_clients += 1
    print(f"[Network] Web client connected. Active clients: {active_clients}")
    
    try:
        while True:
            try:
                # Get JPEG frame from queue (with timeout)
                frame = frame_queue.get(timeout=1.0)
                
                # Track bytes sent
                frame_data = (b'--FRAME\r\n'
                             b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                bytes_sent += len(frame_data)
                
                # Yield frame in multipart format for MJPEG streaming
                yield frame_data
                
            except queue.Empty:
                # No frame available, continue waiting
                continue
                
    except GeneratorExit:
        pass
    finally:
        active_clients -= 1
        print(f"[Network] Web client disconnected. Active clients: {active_clients}")

@app.route('/')
def index():
    """Serve the main web page with embedded camera stream"""
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Jetson Camera Stream + Record</title>
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
                    border: 2px solid #76b900;
                    border-radius: 5px;
                }
                .info {
                    margin-top: 20px;
                    color: #666;
                    font-size: 14px;
                    text-align: left;
                }
                .recording-indicator {
                    display: inline-block;
                    width: 12px;
                    height: 12px;
                    background: #76b900;
                    border-radius: 50%;
                    animation: pulse 1.5s ease-in-out infinite;
                    margin-right: 8px;
                }
                @keyframes pulse {
                    0%, 100% { opacity: 1; }
                    50% { opacity: 0.3; }
                }
                .specs {
                    background: #ecf0f1;
                    padding: 15px;
                    border-radius: 5px;
                    margin-top: 15px;
                }
                .nvidia-badge {
                    color: #76b900;
                    font-weight: bold;
                }
            </style>
        </head>
        <body>
            <h1><span class="recording-indicator"></span>Jetson Camera Stream + Recording</h1>
            <div class="stream-container">
                <h2>Live Web Feed (MJPEG)</h2>
                <img src="{{ url_for('video_feed') }}" alt="Camera Stream">
                <div class="info">
                    <p><strong>Web Stream:</strong> 1280x720 @ 15fps, ~10Mbps</p>
                    <p><strong>Status:</strong> Recording to local storage simultaneously</p>
                    <div class="specs">
                        <h3>Recording Specifications:</h3>
                        <ul>
                            <li><strong>Resolution:</strong> 1920x1080 @ 30fps</li>
                            <li><strong>Codec:</strong> H.264 (hardware encoded via NVENC)</li>
                            <li><strong>Bitrate:</strong> 15Mbps (high quality)</li>
                            <li><strong>Storage:</strong> MP4 files in recordings/</li>
                            <li><strong>Camera:</strong> Pi Camera Module v2 (8MP CSI)</li>
                        </ul>
                        <h3>Hardware Acceleration:</h3>
                        <ul>
                            <li class="nvidia-badge">NVIDIA NVENC</li> - H.264 hardware encoding
                            <li class="nvidia-badge">NVIDIA NVJPEG</li> - JPEG hardware encoding
                            <li class="nvidia-badge">NVIDIA NVMM</li> - Zero-copy memory management
                            <li class="nvidia-badge">Jetson Orin Nano</li> - Optimized for low CPU usage
                        </ul>
                        <p><em>Expected CPU usage: 15-25% (vs 40-55% on Pi Zero 2 W)</em></p>
                    </div>
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
    if pipeline is not None and recording_active:
        return {
            'status': 'running',
            'recording': recording_active,
            'file': current_recording_file,
            'resolution': '1920x1080 @ 30fps',
            'web_stream': '1280x720 @ 15fps',
            'platform': 'Jetson Orin Nano',
            'camera': 'Pi Camera Module v2'
        }, 200
    return {'status': 'stopped'}, 503

if __name__ == '__main__':
    try:
        print("=" * 70)
        print("Jetson Orin Nano Camera Stream + Record Server")
        print("=" * 70)
        print("Initializing camera system with hardware acceleration...")
        
        if not init_camera():
            print("Failed to initialize camera. Exiting.")
            exit(1)
        
        # Start statistics monitoring thread
        monitor_thread = threading.Thread(target=monitor_stats, daemon=True)
        monitor_thread.start()
        
        # Wait a moment for pipeline to stabilize
        time.sleep(1)
        
        print("\n" + "=" * 70)
        print("Camera system ready!")
        print("=" * 70)
        print(f"Web interface: http://<jetson-ip>:8080")
        print(f"Recording to: {current_recording_file}")
        print("Press Ctrl+C to stop")
        print("=" * 70 + "\n")
        
        # Run Flask server
        app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
        
    except KeyboardInterrupt:
        print("\n\n[Shutdown] Ctrl+C detected, stopping camera system...")
    except Exception as e:
        print(f"\n[Error] {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n[Shutdown] Cleaning up resources...")
        stop_recording()
        
        # Additional cleanup time for Argus/camera subsystem
        print("[Shutdown] Releasing camera hardware...")
        time.sleep(1.0)
        
        print("[Shutdown] Camera closed. Goodbye!")
