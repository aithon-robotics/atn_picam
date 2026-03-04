#!/usr/bin/env python3
"""
GMSL2 4-Camera WebRTC Streamer — Jetson Orin Nano Super / Orin NX / AGX Orin
==============================================================================

Hardware:
  - Jetson Orin Nano Super (software-path encoder) **or** Orin NX / AGX Orin
    (hardware NVENC — strongly recommended for production 4-camera use)
  - Technexion VL-GM2-8CAM-RPI22 GMSL2 frame-grabber connected via BOTH CSI
    connectors on the Jetson
  - Up to 4× Technexion AR0234 GMSL2 cameras → appear as /dev/video0–3

⚠  NVENC availability:
  - Jetson Orin Nano / Nano Super : NO dedicated NVENC hardware.
    nvv4l2h264enc falls back to a GPU-compute (software) path.
    Feasible for ≤2 streams at 1280×720@30fps.  For 4 streams upgrade the
    module to at minimum the Jetson Orin NX 8GB.
  - Jetson Orin NX 8 GB / 16 GB  : 1× NVENC (≈4× 1080p30 H.264)
  - Jetson AGX Orin 32 GB / 64 GB : 2× NVENC (≈8× 1080p60 H.264)

Pipeline (per camera, stays fully in GStreamer / NVMM):
  v4l2src → nvvidconv (GPU scale+colour) → nvv4l2h264enc (NVENC / GPU-sw)
           → h264parse → rtph264pay → webrtcbin

vs. previous implementation (single camera, Python-side encoding):
  nvarguscamerasrc → [raw frames pulled into Python] → aiortc libx264 (CPU)

Key improvements:
  • 4 cameras instead of 1
  • Zero Python-side encode — gst-webrtcbin owns the full H.264/RTP/DTLS path
  • v4l2src: compatible with Technexion's GMSL2 V4L2 kernel driver
  • nvvidconv: GPU-accelerated colour-conversion + downscale in one pass (NVMM)
  • Single peer-connection with 4 bundled video tracks (WebRTC bundle)

Dependencies (system packages on JetPack):
  python3-gi gstreamer1.0-tools gstreamer1.0-plugins-base
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
  gstreamer1.0-plugins-ugly gstreamer1.0-rtsp
  gir1.2-gst-plugins-bad-1.0   ← provides GstWebRTC + GstSdp

Python packages:  aiohttp psutil
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

import psutil
from aiohttp import web
from atn_picam.core.storage import check_and_cleanup_storage

log = logging.getLogger(__name__)

Gst.init(None)

# ---------------------------------------------------------------------------
# Defaults — override via CLI args or environment variables
# ---------------------------------------------------------------------------

# V4L2 devices exposed by the Technexion GMSL2 driver.
# Verify on target with: v4l2-ctl --list-devices
CAMERA_DEVICES = [
    os.environ.get("CAM0", "/dev/video0"),
    os.environ.get("CAM1", "/dev/video1"),
    os.environ.get("CAM2", "/dev/video2"),
    os.environ.get("CAM3", "/dev/video3"),
]

# Pixel format reported by the Technexion AR0234 GMSL2 driver.
# Check on target with:  v4l2-ctl -d /dev/video0 --list-formats-ext
# Common values: UYVY, YUY2, NV12, RG10 (raw Bayer 10-bit → needs debayer step)
CAMERA_FORMAT = os.environ.get("CAM_FORMAT", "UYVY")

# Sensor native resolution & frame rate
SENSOR_W = int(os.environ.get("SENSOR_W", 1920))
SENSOR_H = int(os.environ.get("SENSOR_H", 1080))
SENSOR_FPS = int(os.environ.get("SENSOR_FPS", 30))

# WebRTC stream resolution (scaled from sensor res by nvvidconv on GPU)
STREAM_W = int(os.environ.get("STREAM_W", 1280))
STREAM_H = int(os.environ.get("STREAM_H", 720))

# H.264 encoding parameters
# On Orin NX / AGX Orin this goes to NVENC hardware.
# On Orin Nano Super this goes through a GPU-compute path (still fast enough
# at 720p; reduce to 960×540 if CPU pressure is too high).
STREAM_BITRATE = int(os.environ.get("STREAM_BITRATE", 4_000_000))   # 4 Mbps/cam
RECORD_BITRATE = int(os.environ.get("RECORD_BITRATE", 8_000_000))   # 8 Mbps/cam
IFRAME_INTERVAL = int(os.environ.get("IFRAME_INTERVAL", 30))         # 1 s at 30 fps

RECORDINGS_DIR = os.path.expanduser(os.environ.get("RECORDINGS_DIR", "~/recordings"))

# ICE / WebRTC
# For a direct Ethernet cable (no NAT) STUN is not strictly required.
# Host candidates (local IP) are gathered immediately and will work.
# Set to "" to disable STUN entirely.
STUN_SERVER = os.environ.get("STUN_SERVER", "stun://stun.l.google.com:19302")


# ---------------------------------------------------------------------------
# GStreamer pipeline helpers
# ---------------------------------------------------------------------------

def _camera_branch(cam_idx: int, device: str) -> str:
    """
    Return the GStreamer pipeline fragment for one camera.

    The fragment ends with 'sendrecv.' which asks webrtcbin to request a new
    sink pad (sink_0, sink_1, …) — standard GStreamer request-pad behaviour.
    """
    stun = f"stun-server={STUN_SERVER} " if STUN_SERVER else ""
    _ = stun  # used on first camera only (webrtcbin declaration)

    return (
        f"v4l2src device={device} name=cam{cam_idx} ! "
        # Sensor caps — adjust CAMERA_FORMAT / SENSOR_W / SENSOR_H if needed
        f"video/x-raw,format={CAMERA_FORMAT},"
        f"width={SENSOR_W},height={SENSOR_H},framerate={SENSOR_FPS}/1 ! "

        # nvvidconv: GPU-accelerated colour conversion + downscale in one pass.
        # Input stays in NVMM (zero-copy from CSI path once driver supports it).
        f"nvvidconv ! "
        f"video/x-raw(memory:NVMM),format=NV12,"
        f"width={STREAM_W},height={STREAM_H} ! "

        # nvv4l2h264enc:
        #   On Orin NX / AGX Orin  → dedicated NVENC hardware block
        #   On Orin Nano Super      → GPU-compute software path
        # maxperf-enable=1: prevents NVENC from throttling when running multiple
        # encoder instances in parallel.
        f"nvv4l2h264enc name=enc{cam_idx} "
        f"  bitrate={STREAM_BITRATE} "
        f"  maxperf-enable=1 "
        f"  iframeinterval={IFRAME_INTERVAL} "
        f"  insert-sps-pps=true "
        f"  insert-vui=true ! "

        # Normalise to byte-stream AUs before handing to h264parse / RTP layer
        f"video/x-h264,stream-format=byte-stream,alignment=au,profile=baseline ! "
        f"h264parse ! "

        # rtph264pay: config-interval=-1 re-sends SPS/PPS before every IDR so
        # browsers can recover from packet loss without a full reconnect.
        f"rtph264pay name=pay{cam_idx} config-interval=-1 "
        f"  aggregate-mode=zero-latency pt=96 ! "

        # Small queue decouples encoder timing from WebRTC pacing/SRTP
        f"queue name=q{cam_idx} max-size-buffers=2 leaky=2 ! "

        # Link into webrtcbin — 'sendrecv.' requests the next free sink_%u pad
        f"sendrecv. "
    )


def _build_pipeline_str(num_cameras: int) -> str:
    """
    Assemble the full GStreamer pipeline string for *num_cameras* cameras and
    one webrtcbin instance that bundles all streams into a single WebRTC PC.
    """
    stun = f"stun-server={STUN_SERVER} " if STUN_SERVER else ""
    parts = [
        # webrtcbin with max-bundle so all cameras share one DTLS/ICE pair.
        # This dramatically reduces connection overhead on the Jetson.
        f"webrtcbin name=sendrecv bundle-policy=max-bundle {stun}",
    ]
    for i, device in enumerate(CAMERA_DEVICES[:num_cameras]):
        parts.append(_camera_branch(i, device))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Session: one GStreamer pipeline per browser peer connection
# ---------------------------------------------------------------------------

class GMSLSession:
    """
    Manages a single WebRTC peer connection backed by a GStreamer pipeline.

    Lifecycle:
      1. create() — build and start pipeline, complete ICE, return SDP answer
      2. pipeline runs in background (GLib mainloop thread)
      3. cleanup() — stop pipeline, quit GLib loop
    """

    def __init__(self, session_id: str, num_cameras: int):
        self.session_id = session_id
        self.num_cameras = num_cameras
        self.pipeline: Gst.Pipeline | None = None
        self.webrtcbin: Gst.Element | None = None
        self._glib_loop: GLib.MainLoop | None = None
        self._glib_thread: threading.Thread | None = None
        self._ice_complete = threading.Event()
        self._answer_ready = threading.Event()
        self._answer_sdp: str | None = None
        self._on_close_cb = None   # called when ICE fails/disconnects

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, offer_sdp: str, on_close=None) -> str | None:
        """
        Blocking entry point called from asyncio via run_in_executor.

        Builds the GStreamer pipeline, sets the browser's SDP offer as remote
        description, waits for ICE gathering to complete, and returns the SDP
        answer string.  Returns None on failure.
        """
        self._on_close_cb = on_close
        try:
            self._build_pipeline()
            self._start_glib_loop()
            self.pipeline.set_state(Gst.State.PLAYING)
            self._wait_for_playing()

            self._set_remote_description(offer_sdp)
            self._create_answer()

            if not self._answer_ready.wait(timeout=10.0):
                log.error("[%s] Timeout waiting for WebRTC answer", self.session_id)
                return None
            if not self._ice_complete.wait(timeout=15.0):
                log.warning("[%s] ICE gathering timed out — using partial candidates",
                            self.session_id)
            return self._answer_sdp
        except Exception as exc:
            log.exception("[%s] create() failed: %s", self.session_id, exc)
            self.cleanup()
            return None

    def cleanup(self):
        """Stop the GStreamer pipeline and GLib main loop."""
        log.info("[%s] Cleaning up pipeline", self.session_id)
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self._glib_loop:
            self._glib_loop.quit()
            self._glib_loop = None
        if self._glib_thread and self._glib_thread.is_alive():
            self._glib_thread.join(timeout=3.0)

    # ------------------------------------------------------------------
    # Pipeline construction
    # ------------------------------------------------------------------

    def _build_pipeline(self):
        pipeline_str = _build_pipeline_str(self.num_cameras)
        log.info("[%s] Creating pipeline:\n%s", self.session_id, pipeline_str)

        self.pipeline = Gst.parse_launch(pipeline_str)
        if not self.pipeline:
            raise RuntimeError("Gst.parse_launch returned None — check plugin availability")

        self.webrtcbin = self.pipeline.get_by_name("sendrecv")
        if not self.webrtcbin:
            raise RuntimeError("Could not find 'sendrecv' webrtcbin in pipeline")

        # Bus: handle errors and EOS from any element
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        # ICE gathering state — signals when all local candidates are collected
        self.webrtcbin.connect(
            "notify::ice-gathering-state", self._on_ice_gathering_state_change
        )

        # ICE connection state — detect browser disconnect
        self.webrtcbin.connect(
            "notify::ice-connection-state", self._on_ice_connection_state_change
        )

    def _start_glib_loop(self):
        """Run a GLib MainLoop in a daemon thread (required for GStreamer bus/signals)."""
        def _run():
            self._glib_loop = GLib.MainLoop()
            self._glib_loop.run()

        self._glib_thread = threading.Thread(target=_run, daemon=True,
                                             name=f"glib-{self.session_id}")
        self._glib_thread.start()
        time.sleep(0.1)  # give the loop a moment to start

    def _wait_for_playing(self):
        ret, state, _ = self.pipeline.get_state(timeout=5 * Gst.SECOND)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(
                "Pipeline failed to reach PLAYING state. "
                "Check that all /dev/videoX devices exist and nvv4l2h264enc is available."
            )

    # ------------------------------------------------------------------
    # WebRTC signalling
    # ------------------------------------------------------------------

    def _set_remote_description(self, sdp_text: str):
        """Parse and apply the browser's SDP offer."""
        res, sdp_msg = GstSdp.SDPMessage.new()
        GstSdp.sdp_message_parse_buffer(sdp_text.encode(), sdp_msg)

        offer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER, sdp_msg
        )
        promise = Gst.Promise.new()
        self.webrtcbin.emit("set-remote-description", offer, promise)
        promise.wait()
        log.debug("[%s] Remote description set", self.session_id)

    def _create_answer(self):
        """Ask webrtcbin to create an SDP answer."""
        promise = Gst.Promise.new_with_change_func(
            self._on_answer_created, None, None
        )
        self.webrtcbin.emit("create-answer", None, promise)

    def _on_answer_created(self, promise, _elem, _data):
        """GLib callback: answer SDP is ready."""
        reply = promise.get_reply()
        answer = reply.get_value("answer")

        # Commit local description so ICE gathering starts
        set_promise = Gst.Promise.new()
        self.webrtcbin.emit("set-local-description", answer, set_promise)
        set_promise.wait()

        self._answer_sdp = answer.sdp.as_text()
        self._answer_ready.set()
        log.info("[%s] SDP answer created, ICE gathering in progress…", self.session_id)

    # ------------------------------------------------------------------
    # GLib / GStreamer signal callbacks
    # ------------------------------------------------------------------

    def _on_ice_gathering_state_change(self, element, _pspec):
        state = element.get_property("ice-gathering-state")
        log.debug("[%s] ICE gathering state: %s", self.session_id, state)
        if state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            # Re-read local description now that all candidates are in SDP
            local_desc = self.webrtcbin.get_property("local-description")
            if local_desc:
                self._answer_sdp = local_desc.sdp.as_text()
            self._ice_complete.set()

    def _on_ice_connection_state_change(self, element, _pspec):
        state = element.get_property("ice-connection-state")
        log.info("[%s] ICE connection state: %s", self.session_id, state)
        if state in (
            GstWebRTC.WebRTCICEConnectionState.FAILED,
            GstWebRTC.WebRTCICEConnectionState.CLOSED,
        ):
            log.info("[%s] Browser disconnected, scheduling cleanup", self.session_id)
            if self._on_close_cb:
                self._on_close_cb(self.session_id)

    def _on_bus_message(self, _bus, message):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error("[%s] GStreamer error: %s\n%s", self.session_id, err, debug)
            self.cleanup()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            log.warning("[%s] GStreamer warning: %s\n%s", self.session_id, warn, debug)
        elif t == Gst.MessageType.EOS:
            log.info("[%s] Pipeline EOS", self.session_id)
            self.cleanup()


# ---------------------------------------------------------------------------
# HTTP server (signalling only — no media data here)
# ---------------------------------------------------------------------------

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Max-Age"] = "3600"
    return response


class GMSLServer:
    """
    aiohttp HTTP server that handles WebRTC signalling for 4 cameras.

    Only one active peer connection is supported at a time (single operator).
    A new /offer request cleanly tears down any existing session before
    starting a fresh one.
    """

    def __init__(self, num_cameras: int):
        self.num_cameras = num_cameras
        self._active_session: GMSLSession | None = None
        self._session_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def index(self, request):
        path = _template_path("gmsl_webrtc.html")
        with open(path) as f:
            return web.Response(content_type="text/html", text=f.read())

    async def offer(self, request):
        """
        Receive SDP offer from browser, return SDP answer.

        Flow:
          1. Browser creates RTCPeerConnection with N recvonly video transceivers
          2. Browser waits for ICE gathering, sends offer to this endpoint
          3. We tear down any old session, create a new GStreamer pipeline
          4. Return SDP answer (ICE candidates embedded via end-of-gathering wait)
        """
        async with self._session_lock:
            data = await request.json()
            offer_sdp = data.get("sdp")
            if not offer_sdp:
                return web.Response(status=400, text="Missing 'sdp' field")

            # Tear down existing session cleanly
            if self._active_session:
                log.info("New offer received — tearing down existing session")
                old = self._active_session
                self._active_session = None
                await asyncio.get_event_loop().run_in_executor(None, old.cleanup)

            session_id = uuid.uuid4().hex[:8]
            session = GMSLSession(session_id, self.num_cameras)

            log.info("[%s] Processing offer for %d cameras", session_id, self.num_cameras)

            def _on_close(sid):
                # Called from GLib thread when ICE drops
                asyncio.get_event_loop().call_soon_threadsafe(
                    self._handle_session_close, sid
                )

            # Run blocking GStreamer setup in a thread pool executor
            answer = await asyncio.get_event_loop().run_in_executor(
                None, session.create, offer_sdp, _on_close
            )

            if answer is None:
                return web.Response(status=500, text="Failed to create WebRTC answer")

            self._active_session = session
            log.info("[%s] Returning SDP answer", session_id)
            return web.json_response({"sdp": answer, "type": "answer"})

    def _handle_session_close(self, session_id: str):
        if self._active_session and self._active_session.session_id == session_id:
            log.info("[%s] Session closed by ICE disconnect", session_id)
            self._active_session.cleanup()
            self._active_session = None

    async def status(self, request):
        active = self._active_session is not None
        return web.json_response({
            "active_session": active,
            "session_id": self._active_session.session_id if active else None,
            "num_cameras": self.num_cameras,
            "camera_devices": CAMERA_DEVICES[:self.num_cameras],
            "stream_resolution": f"{STREAM_W}x{STREAM_H}",
            "stream_bitrate_mbps": round(STREAM_BITRATE / 1e6, 1),
        })

    async def storage_info(self, request):
        try:
            os.makedirs(RECORDINGS_DIR, exist_ok=True)
            stat = os.statvfs(RECORDINGS_DIR)
            free_gb = (stat.f_bavail * stat.f_frsize) / 1e9
            total_gb = (stat.f_blocks * stat.f_frsize) / 1e9
            return web.json_response({
                "success": True,
                "free_gb": round(free_gb, 2),
                "total_gb": round(total_gb, 2),
            })
        except Exception as exc:
            return web.json_response({"success": False, "message": str(exc)})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_startup(self, app):
        app["monitor_task"] = asyncio.create_task(_monitor_loop(self.num_cameras))

    async def on_shutdown(self, app):
        if "monitor_task" in app:
            app["monitor_task"].cancel()
        if self._active_session:
            await asyncio.get_event_loop().run_in_executor(
                None, self._active_session.cleanup
            )


# ---------------------------------------------------------------------------
# System monitor
# ---------------------------------------------------------------------------

async def _monitor_loop(num_cameras: int):
    old_net = psutil.net_io_counters()
    old_t = time.time()
    log.info("[Monitor] Started (watching %d cameras)", num_cameras)
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            net = psutil.net_io_counters()
            dt = now - old_t
            mbps_up = (net.bytes_sent - old_net.bytes_sent) * 8 / 1e6 / dt
            cpu = psutil.cpu_percent()
            mem = psutil.virtual_memory()
            log.info(
                "[System] CPU: %4.1f%%  RAM: %4.1f%%  Net↑: %5.2f Mbps  "
                "(expected: ~%.0f Mbps for %d cams @ %d Mbps/cam)",
                cpu, mem.percent, mbps_up,
                num_cameras * STREAM_BITRATE / 1e6, num_cameras,
                STREAM_BITRATE / 1e6,
            )
            old_net = net
            old_t = now
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.warning("[Monitor] Error: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _template_path(filename: str) -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "templates", filename
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="GMSL2 4-Camera WebRTC Streamer (Jetson Nano Super / Orin NX / AGX Orin)"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--cameras", type=int, default=4, choices=[1, 2, 3, 4],
        help="Number of GMSL2 cameras to stream (default: 4)"
    )
    parser.add_argument(
        "--width", type=int, default=STREAM_W,
        help=f"Stream width in pixels (default: {STREAM_W})"
    )
    parser.add_argument(
        "--height", type=int, default=STREAM_H,
        help=f"Stream height in pixels (default: {STREAM_H})"
    )
    parser.add_argument(
        "--bitrate", type=int, default=STREAM_BITRATE,
        help=f"H.264 bitrate per camera in bps (default: {STREAM_BITRATE})"
    )
    parser.add_argument(
        "--cam-format", default=CAMERA_FORMAT,
        help=f"V4L2 pixel format from GMSL2 driver (default: {CAMERA_FORMAT})"
    )
    args = parser.parse_args()

    # Apply CLI overrides to module-level defaults so _camera_branch() picks
    # them up without needing them threaded through every call.
    global STREAM_W, STREAM_H, STREAM_BITRATE, CAMERA_FORMAT
    STREAM_W = args.width
    STREAM_H = args.height
    STREAM_BITRATE = args.bitrate
    CAMERA_FORMAT = args.cam_format

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    print("=" * 70)
    print("ATN GMSL2 WebRTC Streamer")
    print("=" * 70)
    print(f"  Cameras       : {args.cameras} × GMSL2 (AR0234)")
    print(f"  Devices       : {CAMERA_DEVICES[:args.cameras]}")
    print(f"  Sensor in     : {SENSOR_W}×{SENSOR_H} @ {SENSOR_FPS}fps {CAMERA_FORMAT}")
    print(f"  Stream out    : {STREAM_W}×{STREAM_H} H.264 @ {STREAM_BITRATE//1000} kbps/cam")
    print(f"  Total bitrate : ~{args.cameras * STREAM_BITRATE // 1_000_000} Mbps "
          f"(Ethernet link — no NAT, STUN optional)")
    print(f"  Encoder       : nvv4l2h264enc (NVENC on Orin NX/AGX; GPU-sw on Nano Super)")
    print(f"  Web UI        : http://{args.host}:{args.port}")
    print("=" * 70)
    print()

    server = GMSLServer(num_cameras=args.cameras)

    app = web.Application(middlewares=[cors_middleware])
    app.on_startup.append(server.on_startup)
    app.on_shutdown.append(server.on_shutdown)
    app.router.add_get("/", server.index)
    app.router.add_post("/offer", server.offer)
    app.router.add_get("/status", server.status)
    app.router.add_get("/storage_info", server.storage_info)

    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
