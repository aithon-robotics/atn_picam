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
from datetime import datetime

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import MediaStreamTrack
import av
from atn_picam.core.storage import check_and_cleanup_storage

# Standard 16:9 resolutions (width, height) to pick from for the WebRTC stream.
# Widths must stay multiples of 32: the raw-frame copy in JetsonStreamTrack.recv()
# slices the buffer using plain width*height math, and a non-aligned width causes
# pyav's plane buffer_size to be padded to a wider stride than we slice for, which
# fails every frame and silently falls back to an all-zero (green, not black) frame.
# The traditional 426x240 / 854x480 pixel counts are NOT multiples of 32 - use the
# widths below instead, which keep the same ~16:9 aspect ratio.
RES_240P = (416, 240)
RES_360P = (640, 360)
RES_480P = (832, 480)
RES_QHD_540P = (960, 540)
RES_720P = (1280, 720)
RES_1080P = (1920, 1080)

# Raw YUV420 frames sent over WebRTC - lower resolution reduces encode/network load
WEBRTC_RESOLUTION = RES_QHD_540P
# Source capture / on-disk recording resolution (H.264, separate tee branch)
RECORDING_RESOLUTION = RES_1080P
# Fragmented MP4: max video lost if the recorder dies without a clean stop
RECORDING_FRAGMENT_MS = 1000
# Stop: wait this long for the tee pad to block before sending EOS directly
RECORDING_BLOCK_TIMEOUT_S = 2
# Stop: wait this long for EOS to reach the filesink before tearing down
RECORDING_FINALIZE_TIMEOUT_S = 10

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
        self.recording_finalized = None  # set when EOS reaches the filesink
        self.recording_stopped = None    # set when the recording bin is removed
        self.latest_frame = None  # Shared frame for all viewers
        self.frame_lock = threading.Lock()  # Protect frame access
        self.loop = None
        self.thread = None
        self.running = False

        self._init_pipeline()

    def _init_pipeline(self):
        print("Initializing Jetson Camera Pipeline with Raw Frame Output...")

        # Main pipeline: Source -> Tee -> WebRTC Branch (Raw frames)
        # WebRTC branch outputs RAW YUV420 frames at WEBRTC_RESOLUTION (no H.264 encoding)
        # Recording branch (dynamically added) uses H.264 at RECORDING_RESOLUTION @ 8Mbps
        # This eliminates the decode/re-encode step for faster WebRTC connection
        pipeline_str = (
            "nvarguscamerasrc sensor-id=0 ! "
            f"video/x-raw(memory:NVMM), width={RECORDING_RESOLUTION[0]}, height={RECORDING_RESOLUTION[1]}, framerate=30/1, format=NV12 ! "
            "nvvidconv flip-method=2 ! "
            "video/x-raw(memory:NVMM), format=NV12 ! "
            "tee name=t "
            # WebRTC Branch (Always active) - Raw frames output
            "t. ! queue max-size-buffers=1 leaky=downstream ! "
            "nvvidconv ! "
            f"video/x-raw, width={WEBRTC_RESOLUTION[0]}, height={WEBRTC_RESOLUTION[1]}, format=I420 ! "
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
        """Callback for appsink to retrieve raw YUV420 frames for WebRTC"""
        sample = sink.emit("pull-sample")
        if sample:
            buf = sample.get_buffer()
            result, map_info = buf.map(Gst.MapFlags.READ)
            if result:
                try:
                    # Copy raw frame data (I420 format: Y plane + U plane + V plane)
                    data = bytes(map_info.data)
                    pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else 0

                    frame_info = {
                        'data': data,
                        'pts': pts,
                        'width': WEBRTC_RESOLUTION[0],
                        'height': WEBRTC_RESOLUTION[1]
                    }

                    # Store frame with thread safety - all viewers can read this
                    with self.frame_lock:
                        self.latest_frame = frame_info
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
        """Get the latest raw YUV420 frame data (thread-safe, non-blocking)"""
        with self.frame_lock:
            return self.latest_frame

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
            #
            # qtmux writes a fragmented MP4 (one fragment per second) and, on a
            # clean EOS, finalises it into a regular MP4. A plain MP4 only gets
            # its index (moov) on a clean EOS, so a power cut or killed service
            # left the whole file unplayable. Fragmented, at most the last ~1 s
            # is lost.
            # first-moov-then-finalise (not the default dash-or-mss mode): with
            # GStreamer 1.20 the default mode writes one trun per sample, which
            # ffmpeg (and everything built on it) mis-reads as garbage frames.
            bin_str = (
                f"queue name=rec_queue max-size-buffers=2 leaky=downstream ! "
                f"nvv4l2h264enc name=recording_enc maxperf-enable=1 bitrate=8000000 iframeinterval=30 insert-sps-pps=true insert-vui=true ! "
                f"h264parse ! "
                f"qtmux fragment-duration={RECORDING_FRAGMENT_MS} fragment-mode=first-moov-then-finalise ! "
                f"filesink location={filename} name=rec_sink"
            )

            self.recording_bin = Gst.parse_bin_from_description(bin_str, True)

            # Signal when EOS reaches the filesink: qtmux has then written
            # everything, so the bin can be torn down without losing data.
            self.recording_finalized = threading.Event()
            finalized = self.recording_finalized

            def on_sink_event(pad, info):
                if info.get_event().type == Gst.EventType.EOS:
                    finalized.set()
                return Gst.PadProbeReturn.OK

            rec_sink = self.recording_bin.get_by_name("rec_sink")
            rec_sink.get_static_pad("sink").add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM, on_sink_event)

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
        
        if self.recording_stopped is not None and not self.recording_stopped.is_set():
            return {"success": True, "file": filename, "message": "Already stopping"}

        # To stop cleanly:
        # 1. Block the tee src pad
        # 2. Unlink it and send EOS into the recording bin
        # 3. Wait until EOS reaches the filesink (qtmux has written everything)
        # 4. Set the bin to NULL and remove it
        rec_bin = self.recording_bin
        tee_pad = self.recording_pad
        bin_sink_pad = rec_bin.get_static_pad("sink")
        finalized = self.recording_finalized
        stopped = threading.Event()
        self.recording_stopped = stopped
        eos_sent = threading.Event()
        eos_lock = threading.Lock()

        def send_eos(unlink):
            with eos_lock:
                if eos_sent.is_set():
                    return
                eos_sent.set()
            if unlink:
                tee_pad.unlink(bin_sink_pad)
                self.tee.release_request_pad(tee_pad)
            bin_sink_pad.send_event(Gst.Event.new_eos())

        def pad_probe_cb(pad, info):
            send_eos(unlink=True)
            return Gst.PadProbeReturn.REMOVE

        def finalize_bin():
            # If no buffers flow (camera stalled) the blocking probe never
            # fires; send EOS anyway so the file still gets finalized.
            if not eos_sent.wait(RECORDING_BLOCK_TIMEOUT_S):
                print("Recording: tee pad never blocked, sending EOS directly")
                tee_pad.remove_probe(probe_id)
                send_eos(unlink=False)
            if not finalized.wait(RECORDING_FINALIZE_TIMEOUT_S):
                print(f"Recording: EOS did not reach filesink within "
                      f"{RECORDING_FINALIZE_TIMEOUT_S}s, file may be truncated")
            rec_bin.set_state(Gst.State.NULL)
            if tee_pad.is_linked():
                tee_pad.unlink(bin_sink_pad)
                self.tee.release_request_pad(tee_pad)
            self.pipeline.remove(rec_bin)
            self.recording_bin = None
            self.recording_active = False
            self.current_recording_file = None
            print(f"Recording finalized: {filename}")
            stopped.set()

        # Add blocking probe to stop data flow
        probe_id = tee_pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, pad_probe_cb)
        threading.Thread(target=finalize_bin, daemon=True).start()

        return {"success": True, "file": filename}

    def wait_recording_stopped(self, timeout):
        """Block until a stop started by stop_recording() has finalized the file."""
        stopped = self.recording_stopped
        if stopped is None:
            return True
        return stopped.wait(timeout)

    def get_status(self):
        return {
            "running": self.running,
            "recording": self.recording_active,
            "file": self.current_recording_file
        }

    def close(self):
        if self.recording_active:
            # Finalize the file before the pipeline goes to NULL, otherwise
            # the EOS never reaches qtmux (service stop / reboot).
            self.stop_recording()
            if not self.wait_recording_stopped(
                    RECORDING_BLOCK_TIMEOUT_S + RECORDING_FINALIZE_TIMEOUT_S + 2):
                print("Recording: finalize timed out during shutdown")
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if self.loop:
            self.loop.quit()

camera_manager = JetsonCameraManager()

class JetsonStreamTrack(VideoStreamTrack):
    """
    WebRTC Video Track that sources raw YUV420 frames from the JetsonCameraManager.
    Frames are passed directly to aiortc for encoding (single encode step).
    """
    def __init__(self):
        super().__init__()
        self.width = WEBRTC_RESOLUTION[0]
        self.height = WEBRTC_RESOLUTION[1]

    async def recv(self):
        """
        Receive raw YUV420 frames and pass to aiortc for encoding.
        """
        pts, time_base = await self.next_timestamp()

        # Get raw frame from camera manager
        frame_info = None
        for _ in range(10):
            frame_info = camera_manager.get_frame()
            if frame_info:
                break
            await asyncio.sleep(0.01)

        if frame_info is None:
            # Return black frame if no data
            frame = av.VideoFrame(width=self.width, height=self.height, format="yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = pts
            frame.time_base = time_base
            return frame

        try:
            # Convert raw I420 data to av.VideoFrame
            # I420 format: Y plane (width*height), U plane (width*height/4), V plane (width*height/4)
            data = frame_info['data']
            width = frame_info['width']
            height = frame_info['height']

            # Calculate plane sizes
            y_size = width * height
            uv_size = (width // 2) * (height // 2)

            # Create VideoFrame from raw bytes
            frame = av.VideoFrame(width=width, height=height, format='yuv420p')

            # Copy data to frame planes
            y_end = y_size
            u_end = y_end + uv_size
            v_end = u_end + uv_size

            frame.planes[0].update(data[:y_end])
            frame.planes[1].update(data[y_end:u_end])
            frame.planes[2].update(data[u_end:v_end])

            frame.pts = pts
            frame.time_base = time_base
            return frame

        except Exception as e:
            print(f"Error processing raw frame: {e}")
            # Return black frame on error
            frame = av.VideoFrame(width=self.width, height=self.height, format='yuv420p')
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
    if result.get("success"):
        # Answer only once the file is finalized, so "stopped" in the UI means
        # the file is complete and a new recording can start right away.
        loop = asyncio.get_running_loop()
        done = await loop.run_in_executor(
            None, camera_manager.wait_recording_stopped,
            RECORDING_BLOCK_TIMEOUT_S + RECORDING_FINALIZE_TIMEOUT_S + 2)
        if not done:
            result["message"] = "Stop requested, finalize still in progress"
    return web.json_response(result)

async def storage_info(request):
    try:
        recordings_dir = os.path.expanduser("~/recordings")
        os.makedirs(recordings_dir, exist_ok=True)
        stat = os.statvfs(recordings_dir)
        free_space_gb = (stat.f_bavail * stat.f_frsize) / 1e9  # decimal GB, matches rosbag_record_node
        total_space_gb = (stat.f_blocks * stat.f_frsize) / 1e9
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
