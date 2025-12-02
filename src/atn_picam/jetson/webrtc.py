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
- nvv4l2h264enc: Hardware H.264 encoding for BOTH WebRTC streaming and recording
- nvvidconv: Hardware scaling and color conversion
- maxperf-enable: Optimized for dual encoder instances
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
from aiortc.mediastreams import MediaStreamTrack
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
    nvarguscamerasrc -> tee -> [queue -> nvvidconv -> nvv4l2h264enc -> appsink (WebRTC H.264)]
                            -> [queue -> nvv4l2h264enc -> filesink (Recording)]

    Both branches use hardware H.264 encoding with maxperf-enable for optimal dual-encoder performance.
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
        print("Initializing Jetson Camera Pipeline with Hardware H.264 Encoding...")

        # Main pipeline: Source -> Tee -> WebRTC Branch (H.264 encoded)
        # WebRTC branch now uses hardware H.264 encoding at 960x540 @ 3Mbps
        # Recording branch (dynamically added) uses H.264 at 1920x1080 @ 8Mbps
        # Both use maxperf-enable=1 for optimal dual-encoder performance
        pipeline_str = (
            "nvarguscamerasrc sensor-id=0 ! "
            "video/x-raw(memory:NVMM), width=1920, height=1080, framerate=30/1, format=NV12 ! "
            "nvvidconv flip-method=2 ! "
            "video/x-raw(memory:NVMM), format=NV12 ! "
            "tee name=t "
            # WebRTC Branch (Always active) - Hardware H.264 encoded
            "t. ! queue max-size-buffers=1 leaky=downstream ! "
            "nvvidconv ! "
            "video/x-raw(memory:NVMM), width=960, height=540, format=NV12 ! "
            "nvv4l2h264enc name=webrtc_enc maxperf-enable=1 bitrate=3000000 iframeinterval=30 insert-sps-pps=true insert-vui=true !"
            "video/x-h264, stream-format=byte-stream ! "
            "h264parse ! "
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
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                print("Error: Failed to set pipeline to PLAYING state. Check if nvargus-daemon is running.")
                return

            # Wait for state change to complete or fail
            ret, state, pending = self.pipeline.get_state(5 * Gst.SECOND)
            if ret == Gst.StateChangeReturn.FAILURE:
                print("Error: Pipeline failed to start (async failure). Check nvargus-daemon.")
                self.pipeline.set_state(Gst.State.NULL)
                return
            
            self.running = True
            # GStreamer requires a GLib MainLoop for bus messages and some elements
            self.thread = threading.Thread(target=self._run_glib_loop, daemon=True)
            self.thread.start()
            print("Camera Pipeline started.")

    def _run_glib_loop(self):
        self.loop = GLib.MainLoop()
        try:
            self.loop.run()
        except Exception as e:
            print(f"GLib loop error: {e}")

    def _on_new_sample(self, sink):
        """Callback for appsink to retrieve H.264 encoded packets for WebRTC"""
        sample = sink.emit("pull-sample")
        if sample:
            buf = sample.get_buffer()
            # Map the buffer to read H.264 data
            result, map_info = buf.map(Gst.MapFlags.READ)
            if result:
                try:
                    # Copy H.264 packet data
                    # H.264 packets are much smaller than raw frames (~10-50 KB vs 778 KB)
                    data = bytes(map_info.data)

                    # Get PTS (presentation timestamp) from buffer
                    pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else 0

                    # Store both data and timestamp
                    packet_info = {
                        'data': data,
                        'pts': pts,
                        'size': len(data)
                    }

                    # Put in queue, replace old packet if full (leaky)
                    if self.frame_queue.full():
                        try:
                            self.frame_queue.get_nowait()
                        except queue.Empty:
                            pass
                    self.frame_queue.put_nowait(packet_info)
                finally:
                    buf.unmap(map_info)
        return Gst.FlowReturn.OK

    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            print("End of Stream")
            self.close()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Error: {err}, {debug}")
            self.close()

    def get_frame(self):
        """Get the latest H.264 packet data"""
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
            # queue -> nvv4l2h264enc -> h264parse -> qtmux -> filesink
            # Using maxperf-enable=1 for optimal dual-encoder performance
            # Recording at full resolution (1920x1080) with higher bitrate (8 Mbps)
            bin_str = (
                f"queue name=rec_queue max-size-buffers=2 leaky=downstream ! "
                f"nvv4l2h264enc name=recording_enc maxperf-enable=1 bitrate=8000000 iframeinterval=30 insert-sps-pps=true insert-vui=true ! "
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
    WebRTC Video Track that sources H.264 encoded packets from the JetsonCameraManager.
    This now streams pre-encoded H.264 data, significantly reducing CPU usage.
    """
    def __init__(self):
        super().__init__()
        self.width = 960
        self.height = 540
        self.codec = None
        self._start = None
        self.last_frame = None

    async def recv(self):
        """
        Receive and decode H.264 packets, returning decoded frames to aiortc.
        aiortc will re-encode using the negotiated codec (typically VP8/VP9/H.264).

        Note: We could pass H.264 packets directly if we implement proper RTP packetization,
        but for simplicity we decode here and let aiortc handle re-encoding.
        Future optimization: Implement H264 RTP payloader to pass packets directly.
        """
        pts, time_base = await self.next_timestamp()

        # Initialize codec on first call
        if self.codec is None:
            self.codec = av.CodecContext.create("h264", "r")
            # Don't set width/height - let decoder figure it out from stream
            # Enable error concealment
            self.codec.options = {'flags': 'low_delay'}
            self.codec.open()

        # Get H.264 packet from camera manager
        packet_info = None
        for _ in range(10):
            packet_info = camera_manager.get_frame()
            if packet_info:
                break
            await asyncio.sleep(0.01)

        if packet_info is None:
            # Return last good frame or black frame if no data
            if self.last_frame:
                self.last_frame.pts = pts
                self.last_frame.time_base = time_base
                return self.last_frame

            frame = av.VideoFrame(width=self.width, height=self.height, format="yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = pts
            frame.time_base = time_base
            return frame

        # Decode H.264 packet
        try:
            h264_data = packet_info['data']
            packet = av.Packet(h264_data)
            packet.pts = packet_info['pts']
            packet.dts = packet_info['pts']

            frames = self.codec.decode(packet)

            if frames:
                # Use the first decoded frame
                decoded_frame = frames[0]
                decoded_frame.pts = pts
                decoded_frame.time_base = time_base
                self.last_frame = decoded_frame  # Cache for next time
                return decoded_frame
            else:
                # No frame decoded yet (might need more data or be waiting for keyframe)
                # Return last good frame or black frame
                if self.last_frame:
                    self.last_frame.pts = pts
                    self.last_frame.time_base = time_base
                    return self.last_frame

                frame = av.VideoFrame(width=self.width, height=self.height, format="yuv420p")
                for plane in frame.planes:
                    plane.update(bytes(plane.buffer_size))
                frame.pts = pts
                frame.time_base = time_base
                return frame

        except av.InvalidDataError as e:
            # Invalid H.264 data - likely waiting for keyframe (SPS/PPS)
            # Silently return last good frame or black frame
            if self.last_frame:
                self.last_frame.pts = pts
                self.last_frame.time_base = time_base
                return self.last_frame

            frame = av.VideoFrame(width=self.width, height=self.height, format="yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = pts
            frame.time_base = time_base
            return frame
        except Exception as e:
            # Other errors - log once and return black frame
            if not hasattr(self, '_error_logged'):
                print(f"Error decoding H.264 packet: {e}")
                self._error_logged = True

            if self.last_frame:
                self.last_frame.pts = pts
                self.last_frame.time_base = time_base
                return self.last_frame

            frame = av.VideoFrame(width=self.width, height=self.height, format="yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
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
