#!/usr/bin/env python3
"""
Jetson Orin Nano WebRTC Streamer
--------------------------------
This script implements a low-latency WebRTC streaming solution for NVIDIA Jetson.
It uses:
- aiortc: For the WebRTC stack
- aiohttp: For the signaling web server
- GStreamer: For hardware-accelerated capture and encoding

Hardware Acceleration:
- nvarguscamerasrc: ISP-processed camera capture
- nvv4l2h264enc: Hardware H.264 encoding for recording
- nvvidconv: Hardware scaling and color conversion
"""

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
import psutil
import threading
import queue
from datetime import datetime

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
import av
from atn_picam.core.storage import check_and_cleanup_storage

# Initialize GStreamer
Gst.init(None)

# CORS middleware
@web.middleware
async def cors_middleware(request, handler):
    if request.method == 'OPTIONS':
        response = web.Response()
    else:
        response = await handler(request)
    
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Max-Age'] = '3600'
    return response

class JetsonCameraManager:
    """
    Manages the GStreamer pipeline for simultaneous WebRTC streaming and Recording.
    
    Pipeline Structure:
    nvarguscamerasrc -> tee -> [queue -> nvvidconv -> appsink (WebRTC)]
                            -> [queue -> nvv4l2h264enc -> filesink (Recording)]
    """
    def __init__(self):
        self.pipeline = None
        self.appsink = None
        self.tee = None
        self.recording_bin = None
        self.recording_active = False
        self.current_recording_file = None
        self.frame_queue = queue.Queue(maxsize=1)
        self.loop = None
        self.thread = None
        self.running = False
        
        self._init_pipeline()

    def _init_pipeline(self):
        print("Initializing Jetson Camera Pipeline...")
        
        # Main pipeline: Source -> Tee -> WebRTC Branch
        # We use a high resolution for the source (1080p) to allow good quality recording
        # We downscale for WebRTC to 960x540 (qHD) to save bandwidth/CPU
        pipeline_str = (
            "nvarguscamerasrc sensor-id=0 ! "
            "video/x-raw(memory:NVMM), width=1920, height=1080, framerate=30/1, format=NV12 ! "
            "nvvidconv flip-method=2 ! "
            "video/x-raw(memory:NVMM), format=NV12 ! "
            "tee name=t "
            # WebRTC Branch (Always active)
            "t. ! queue max-size-buffers=1 leaky=downstream ! "
            "nvvidconv ! "
            "video/x-raw, format=I420, width=960, height=540 ! "
            "appsink name=appsink emit-signals=true max-buffers=1 drop=true"
        )
        
        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
            
            self.appsink = self.pipeline.get_by_name("appsink")
            self.appsink.connect("new-sample", self._on_new_sample)
            
            self.tee = self.pipeline.get_by_name("t")
            
            # Bus watch
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_bus_message)
            
            self.start()
            
        except Exception as e:
            print(f"Failed to create pipeline: {e}")

    def start(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.PLAYING)
            self.running = True
            # GStreamer requires a GLib MainLoop for bus messages and some elements
            self.thread = threading.Thread(target=self._run_glib_loop, daemon=True)
            self.thread.start()
            print("Camera Pipeline started.")

    def _run_glib_loop(self):
        self.loop = GLib.MainLoop()
        self.loop.run()

    def _on_new_sample(self, sink):
        """Callback for appsink to retrieve raw frames for WebRTC"""
        sample = sink.emit("pull-sample")
        if sample:
            buf = sample.get_buffer()
            # Map the buffer to read data
            result, map_info = buf.map(Gst.MapFlags.READ)
            if result:
                try:
                    # Copy data to avoid memory issues when GStreamer unmaps
                    data = bytes(map_info.data)
                    # Put in queue, replace old frame if full (leaky)
                    if self.frame_queue.full():
                        try:
                            self.frame_queue.get_nowait()
                        except queue.Empty:
                            pass
                    self.frame_queue.put_nowait(data)
                finally:
                    buf.unmap(map_info)
        return Gst.FlowReturn.OK

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            print("End of Stream")
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Error: {err}, {debug}")

    def get_frame(self):
        """Get the latest raw frame data"""
        try:
            return self.frame_queue.get_nowait()
        except queue.Empty:
            return None

    def start_recording(self):
        if self.recording_active:
            return {"success": False, "message": "Recording already active"}
        
        try:
            recordings_dir = os.path.expanduser("~/recordings")
            os.makedirs(recordings_dir, exist_ok=True)
            check_and_cleanup_storage(recordings_dir)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(recordings_dir, f"jetson_webrtc_{timestamp}.mp4")
            
            print(f"Starting recording to {filename}...")
            
            # Create a bin for recording
            # queue -> nvvidconv -> nvv4l2h264enc -> h264parse -> qtmux -> filesink
            # We use nvvidconv again to ensure format compatibility if needed, 
            # though tee output is already NV12(NVMM).
            bin_str = (
                f"queue name=rec_queue ! "
                f"nvv4l2h264enc bitrate=8000000 iframeinterval=30 ! "
                f"h264parse ! qtmux ! filesink location={filename} name=rec_sink"
            )
            
            self.recording_bin = Gst.parse_bin_from_description(bin_str, True)
            self.pipeline.add(self.recording_bin)
            self.recording_bin.sync_state_with_parent()
            
            # Link tee to recording bin
            tee_src_pad = self.tee.request_pad_simple("src_%u")
            bin_sink_pad = self.recording_bin.get_static_pad("sink")
            
            if tee_src_pad.link(bin_sink_pad) != Gst.PadLinkReturn.OK:
                print("Failed to link recording bin")
                self.pipeline.remove(self.recording_bin)
                self.recording_bin = None
                return {"success": False, "message": "Pipeline link failed"}
            
            self.recording_active = True
            self.current_recording_file = filename
            self.recording_pad = tee_src_pad # Store pad to unlink later
            
            return {"success": True, "file": filename}
            
        except Exception as e:
            print(f"Start recording failed: {e}")
            return {"success": False, "message": str(e)}

    def stop_recording(self):
        if not self.recording_active or not self.recording_bin:
            return {"success": False, "message": "No active recording"}
        
        filename = self.current_recording_file
        print(f"Stopping recording: {filename}")
        
        # To stop cleanly:
        # 1. Block the tee src pad
        # 2. Send EOS to the recording bin
        # 3. Unlink and remove bin
        
        def pad_probe_cb(pad, info, user_data):
            # Remove the probe
            pad.remove_probe(info.id)
            
            # Unlink
            sink_pad = self.recording_bin.get_static_pad("sink")
            pad.unlink(sink_pad)
            self.tee.release_request_pad(pad)
            
            # Send EOS to bin to finalize file
            self.recording_bin.send_event(Gst.Event.new_eos())
            
            # Wait for EOS message on the bus (handled in _on_bus_message? No, that's global)
            # We can just wait a bit or use a bus watch for the bin. 
            # For simplicity in this script, we'll wait a short duration then set to NULL.
            # A better way is to wait for the EOS to propagate to the sink.
            
            def finalize_bin():
                time.sleep(1.0) # Give time for muxer to write index
                self.recording_bin.set_state(Gst.State.NULL)
                self.pipeline.remove(self.recording_bin)
                self.recording_bin = None
                self.recording_active = False
                self.current_recording_file = None
                print("Recording bin removed.")
                
            threading.Thread(target=finalize_bin).start()
            
            return Gst.PadProbeReturn.DROP

        # Add blocking probe to stop data flow
        self.recording_pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, pad_probe_cb, None)
        
        return {"success": True, "file": filename}

    def get_status(self):
        return {
            "running": self.running,
            "recording": self.recording_active,
            "file": self.current_recording_file
        }

    def close(self):
        if self.recording_active:
            self.stop_recording()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if self.loop:
            self.loop.quit()

camera_manager = JetsonCameraManager()

class JetsonStreamTrack(VideoStreamTrack):
    """
    WebRTC Video Track that sources frames from the JetsonCameraManager.
    """
    def __init__(self):
        super().__init__()
        self.width = 960
        self.height = 540
        # GStreamer I420 is YUV420P
        self.format = "yuv420p" 

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        # Get frame from camera manager
        # We run this in executor if it was blocking, but queue.get_nowait is fast.
        # However, we might want to wait for a frame?
        # aiortc expects us to return a frame. If we return too fast without data, it spins.
        
        # Simple spin-wait for frame (since we don't have async queue)
        # In production, use an asyncio.Queue fed by the GStreamer callback
        frame_data = None
        for _ in range(10):
            frame_data = camera_manager.get_frame()
            if frame_data:
                break
            await asyncio.sleep(0.01)
            
        if frame_data is None:
            # Return black frame if no data
            frame = av.VideoFrame(width=self.width, height=self.height, format=self.format)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = pts
            frame.time_base = time_base
            return frame

        # Create VideoFrame from raw bytes
        # I420 layout: Y plane (w*h) + U plane (w/2*h/2) + V plane (w/2*h/2)
        frame = av.VideoFrame(width=self.width, height=self.height, format=self.format)
        
        # Copy data into planes
        # GStreamer I420 is contiguous: Y...U...V...
        y_size = self.width * self.height
        uv_size = (self.width // 2) * (self.height // 2)
        
        frame.planes[0].update(frame_data[0:y_size])
        frame.planes[1].update(frame_data[y_size:y_size+uv_size])
        frame.planes[2].update(frame_data[y_size+uv_size:])
        
        frame.pts = pts
        frame.time_base = time_base
        return frame

# --- Web Server & API (Identical to Pi Zero implementation) ---

def get_template_path(filename):
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates', filename)

async def index(request):
    with open(get_template_path('webrtc_index.html'), 'r') as f:
        content = f.read()
    return web.Response(content_type="text/html", text=content)

async def embed(request):
    with open(get_template_path('webrtc_embed.html'), 'r') as f:
        content = f.read()
    return web.Response(content_type="text/html", text=content)

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

    try:
        video_track = JetsonStreamTrack()
        pc.addTrack(video_track)
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
    try:
        if camera_manager.recording_active:
            camera_manager.stop_recording()
        return web.json_response({"success": True, "message": "Recording state reset"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)})

async def monitor_loop():
    process = psutil.Process()
    old_net_io = psutil.net_io_counters()
    last_time = time.time()
    print("[Monitor] System monitoring started...")
    while True:
        try:
            await asyncio.sleep(3)
            current_time = time.time()
            dt = current_time - last_time
            cpu_percent = psutil.cpu_percent()
            net_io = psutil.net_io_counters()
            bytes_sent = net_io.bytes_sent - old_net_io.bytes_sent
            bytes_recv = net_io.bytes_recv - old_net_io.bytes_recv
            mbps_sent = (bytes_sent * 8) / (1000 * 1000 * dt)
            mbps_recv = (bytes_recv * 8) / (1000 * 1000 * dt)
            print(f"[System] CPU: {cpu_percent:5.1f}% | Net Up: {mbps_sent:5.2f} Mbps")
            old_net_io = net_io
            last_time = current_time
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Monitor] Error: {e}")

async def on_startup(app):
    app['monitor_task'] = asyncio.create_task(monitor_loop())

async def on_shutdown(app):
    if 'monitor_task' in app:
        app['monitor_task'].cancel()
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()
    if camera_manager:
        camera_manager.close()

pcs = set()

def main():
    parser = argparse.ArgumentParser(description="Jetson WebRTC Streamer")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    app = web.Application(middlewares=[cors_middleware])
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/embed", embed)
    app.router.add_post("/offer", offer)
    app.router.add_post("/start_recording", start_recording)
    app.router.add_post("/stop_recording", stop_recording)
    app.router.add_get("/storage_info", storage_info)
    app.router.add_get("/status", status)
    app.router.add_post("/reset_recording", reset_recording_state)

    print(f"Starting Jetson WebRTC server at http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
