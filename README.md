# ATN PiCam: Multi-Platform Camera Streaming & Recording

A unified Python package for high-performance camera streaming and recording on **NVIDIA Jetson** and **Raspberry Pi** devices.

## 🎯 Overview

This project provides simultaneous high-quality video recording and web streaming. It abstracts hardware differences to provide a consistent interface across platforms.

- **Raspberry Pi (Zero 2 W / 3 / 4 / 5)**: Uses `picamera2` and hardware encoders.
- **NVIDIA Jetson + GMSL2**: Uses GStreamer `gst-webrtcbin` for a fully hardware pipeline — zero Python-side encoding.

---

## 🔌 Current Production Hardware (GMSL2 Setup)

| Component | Part |
|-----------|------|
| **Compute** | NVIDIA Jetson Orin Nano Super Dev Board |
| **Camera** | Technexion UVLS-GM2-AR0234 (AR0234, 2.3MP, GMSL2) |
| **Capture board** | Technexion VL-GM2-8CAM-RPI22 (8-camera GMSL2 frame-grabber) |
| **Interface** | GMSL2 serial → VL-GM2-8CAM-RPI22 → **both CSI connectors** on Jetson |
| **Network** | Direct Gigabit Ethernet (Jetson to operator station) |

### ⚠️ Critical: NVENC availability per Jetson module

The Jetson Orin Nano (and Nano Super) **do not have a dedicated NVENC hardware block**.
`nvv4l2h264enc` falls back to a GPU-compute software path on these modules.

| Module | Hardware NVENC | Max concurrent H.264 encode | Production recommendation |
|--------|---------------|------------------------------|--------------------------|
| **Jetson Orin Nano / Nano Super** | ❌ None (GPU-compute sw) | ~2× 720p30 comfortable | Development / 1-2 cams only |
| **Jetson Orin NX 8 GB** | ✅ 1× NVENC | ~4× 1080p30 | **Minimum for 4-cam production** |
| **Jetson Orin NX 16 GB** | ✅ 1× NVENC | ~4× 1080p60 | 4-cam with recording headroom |
| **Jetson AGX Orin 32/64 GB** | ✅ 2× NVENC | ~8× 1080p60 | All 8 cameras at full rate |

> **Recommended upgrade path for 4-camera production**: swap the SOM to a
> **Jetson Orin NX 8 GB**.  The carrier board (Dev Kit) and the VL-GM2-8CAM-RPI22
> CSI connections remain unchanged.  The NX gives you a real hardware NVENC engine
> capable of 4× 1080p30 H.264 with plenty of headroom for simultaneous recording.

---

## ✨ Features

| Feature | Raspberry Pi | Jetson + GMSL2 (new) |
|---------|-------------|----------------------|
| **Cameras** | 1 | **1–4 GMSL2** (AR0234 via VL-GM2-8CAM-RPI22) |
| **Recording** | 1920×1080 @ 30fps | 1920×1080 @ 30fps per cam |
| **Streaming** | 1280×720 MJPEG | **1280×720 H.264 @ 30fps × 4** |
| **WebRTC** | ✅ aiortc | ✅ **gst-webrtcbin (zero Python encode)** |
| **Encoder** | VideoCore HW | **nvv4l2h264enc → NVENC on Orin NX/AGX** |
| **CPU Usage** | ~40-55% | **~5-12%** (Orin NX, hardware NVENC) |
| **Web UI** | Single camera | **4-camera 2×2 grid** |
| **Network** | WiFi / Ethernet | **Direct GbE, no STUN/TURN required** |

## 🚀 Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/aithon-robotics/atn_picam.git
    cd atn_picam
    ```

2.  **Create a virtual environment (Recommended):**
    ```bash
    python3 -m venv venv --system-site-packages
    source venv/bin/activate
    ```

3.  **Install the package:**
    ```bash
    pip install -e .
    ```

## 🎮 Usage

The package installs several command-line tools for easy access.

### 🟢 Raspberry Pi

*   **Stream + Record (MJPEG):**
    ```bash
    atn-pizero-stream
    ```
    *   Access at `http://<pi-ip>:8080`
    *   Records to `~/recordings/`

*   **WebRTC Streaming (Low Latency):**
    ```bash
    atn-pizero-webrtc
    ```
    *   Access at `http://<pi-ip>:8080`
    *   Records to `~/recordings/`

*   **Remote Start from Steam Deck (or any device):**
    ```bash
    ssh atnp1 "/home/atn/aithon_code/atn_picam/venv/bin/python3 -m atn_picam.pizero.webrtc"
    ```
    *   Starts WebRTC streaming remotely via SSH
    *   Replace `atnp1` with your Pi's hostname or IP
    *   Use Ctrl+C to stop
    *   On Steamdeck, the joystick bash script starts the cameras automatically.

### 🟢 NVIDIA Jetson — GMSL2 4-Camera (Production)

*   **4-Camera GMSL2 WebRTC (Recommended — gst-webrtcbin, zero Python encode):**
    ```bash
    atn-jetson-gmsl-webrtc
    # or with options:
    atn-jetson-gmsl-webrtc --cameras 4 --width 1280 --height 720 --bitrate 4000000
    ```
    *   Access at `http://<jetson-ip>:8080`
    *   Streams 4 GMSL2 cameras in a 2×2 grid via a single WebRTC peer connection
    *   Full GStreamer hardware pipeline — no Python-side encoding overhead
    *   `--cameras N` to stream only N cameras (1–4)
    *   For Orin Nano Super, reduce to `--width 960 --height 540` if GPU-compute
        encoder is under pressure

*   **GMSL2 Camera Setup (one-time):**
    ```bash
    # 1. Install Technexion kernel module for VL-GM2-8CAM-RPI22
    #    Follow Technexion BSP installation guide for your JetPack version.

    # 2. Verify cameras appear as V4L2 devices:
    v4l2-ctl --list-devices

    # 3. Check the pixel format your driver outputs:
    v4l2-ctl -d /dev/video0 --list-formats-ext
    # Default assumed: UYVY 1920×1080@30fps
    # Override with env var if different:
    CAM_FORMAT=YUY2 atn-jetson-gmsl-webrtc

    # 4. Required GStreamer plugins (install if missing):
    sudo apt install gstreamer1.0-plugins-bad gir1.2-gst-plugins-bad-1.0
    # Verify webrtcbin is available:
    gst-inspect-1.0 webrtcbin
    ```

*   **Environment variable overrides:**
    ```bash
    CAM0=/dev/video0   # Camera 0 device (default: /dev/video0)
    CAM1=/dev/video1   # Camera 1 device
    CAM2=/dev/video2   # Camera 2 device
    CAM3=/dev/video3   # Camera 3 device
    CAM_FORMAT=UYVY    # V4L2 pixel format from GMSL2 driver
    SENSOR_W=1920      # Native sensor width
    SENSOR_H=1080      # Native sensor height
    STREAM_W=1280      # WebRTC stream width (GPU-scaled)
    STREAM_H=720       # WebRTC stream height
    STREAM_BITRATE=4000000  # H.264 bitrate per camera (bps)
    STUN_SERVER=""     # Set to empty to disable STUN (direct Ethernet)
    ```

### 🟢 NVIDIA Jetson — Legacy single-camera

*   **Stream + Record (MJPEG):**
    ```bash
    atn-jetson-stream
    ```
    *   Access at `http://<jetson-ip>:8080`
    *   Records to `~/recordings/`

*   **WebRTC Streaming (single camera, aiortc):**
    ```bash
    atn-jetson-webrtc
    ```
    *   Access at `http://<jetson-ip>:8080`
    *   Uses `nvarguscamerasrc` (requires NVIDIA-native camera, not GMSL2)
    *   Records to `~/recordings/`

## ⚙️ Auto-Start Service Setup

To make the camera start automatically on boot, use the provided scripts.

1.  **Navigate to the scripts directory:**
    ```bash
    cd scripts
    ```

2.  **Install the desired service:**

    *   **For Jetson Stream:**
        ```bash
        sudo ./setup_services.sh jetson_stream.service
        ```

    *   **For Jetson WebRTC (Recommended - Hardware Accelerated):**
        ```bash
        sudo ./setup_services.sh jetson_webrtc.service
        ```

    *   **For Pi Zero Stream:**
        ```bash
        sudo ./setup_services.sh picam_stream.service
        ```

    *   **For Pi Zero WebRTC:**
        ```bash
        sudo ./setup_services.sh picam_webrtc.service
        ```

3.  **Manage the service:**
    ```bash
    sudo systemctl status <service_name>
    sudo systemctl stop <service_name>
    sudo systemctl restart <service_name>
    sudo sysetmctl disable <service_name>
    ```

## 🔧 Architecture & Implementation

### Raspberry Pi
*   **Library:** `picamera2`
*   **Pipeline:** The ISP splits the camera sensor data into two streams:
    1.  **Main (High Res):** Goes to the H.264 hardware encoder -> File.
    2.  **LoRes (Low Res):** Goes to the MJPEG hardware encoder -> Web Stream.

### NVIDIA Jetson — GMSL2 4-Camera WebRTC (Production, `gmsl_webrtc.py`)

This is the production implementation for the Technexion GMSL2 hardware setup.

*   **Stack:** GStreamer `gst-webrtcbin` + Python `aiohttp` signalling
*   **Zero Python-side encoding:** The entire H.264 encode → RTP → DTLS path
    stays inside GStreamer. Python only handles the HTTP offer/answer exchange.

```
GMSL2 Camera 0 (/dev/video0)
  └─ v4l2src
      └─ nvvidconv  ← GPU color-convert UYVY→NV12 + downscale (NVMM zero-copy)
          └─ nvv4l2h264enc  ← NVENC hardware (Orin NX/AGX) or GPU-sw (Nano Super)
              └─ h264parse → rtph264pay ──┐
                                          │
GMSL2 Camera 1 (/dev/video1)             │
  └─ (same chain) ─────────────────────── webrtcbin (bundle-policy=max-bundle)
                                          │     ↕ DTLS-SRTP over ICE (Ethernet)
GMSL2 Camera 2 (/dev/video2)             │
  └─ (same chain) ──────────────────────►│
                                          │
GMSL2 Camera 3 (/dev/video3)             │
  └─ (same chain) ──────────────────────►│
                                          ↓
                               Browser (4× <video> elements)
                               RTCPeerConnection with 4 recvonly tracks
```

*   **Key design choices:**
    - `v4l2src` instead of `nvarguscamerasrc` — GMSL2 cameras arrive via
      Technexion's V4L2 kernel driver, not the NVIDIA Argus ISP path.
    - `bundle-policy=max-bundle` — all 4 streams share one DTLS/ICE pair,
      reducing connection overhead on the Jetson.
    - `rtph264pay config-interval=-1` — SPS/PPS re-sent before every IDR
      so the browser can recover from any packet loss without reconnecting.
    - `maxperf-enable=1` on each encoder — prevents NVENC throttling when
      multiple encoder instances run simultaneously.
    - ICE gather-and-wait — the signalling server waits for ICE gathering
      to complete before returning the SDP answer. On a direct Ethernet
      link (no NAT) host candidates are collected in < 200 ms.
    - Single active session — designed for one operator at a time; a new
      `/offer` request cleanly tears down the previous session.

*   **Bandwidth (4 cameras, default settings):**

    | Parameter | Value |
    |-----------|-------|
    | Stream resolution | 1280×720 per camera |
    | Bitrate per camera | 4 Mbps H.264 |
    | Total stream bitrate | ~16 Mbps |
    | GbE link capacity | 1000 Mbps |
    | Link utilisation | ~1.6% |

*   **CPU usage (Jetson Orin NX, hardware NVENC):** ~5–12%
*   **Latency:** < 100 ms glass-to-glass over direct Ethernet

### NVIDIA Jetson — Legacy single-camera (`stream.py` / `webrtc.py`)

*   **Source:** `nvarguscamerasrc` (NVIDIA Argus ISP — native cameras only, not GMSL2)
*   **WebRTC library:** aiortc (Python-side H.264 encode via libx264)
*   **Kept for reference/compatibility; not the production path for GMSL2 hardware.**

## 🛠 Troubleshooting

### Camera Setup

*   **Camera not detected on Raspberry Pi?**
    1.  Check cable connection (blue side faces USB ports)
    2.  Enable camera: `sudo raspi-config` → Interface Options → Camera
    3.  Verify detection: `vcgencmd get_camera` (should show `detected=1`)
    4.  Check logs: `dmesg | grep -iE "camera|imx219|imx708"`

*   **Manual camera overlay configuration:**
    
    For IMX219 or IMX708 cameras that don't auto-detect, add to `/boot/firmware/config.txt`:
    ```bash
    # For Camera Module 3 (IMX708)
    dtoverlay=imx708
    
    # For IMX219-200
    dtoverlay=imx219
    ```
    Then reboot: `sudo reboot`
    
    **Important:** Only use ONE overlay at a time. Having both enabled simultaneously causes conflicts and prevents camera detection.
    
    **To swap cameras:**
    1. Power off the Pi
    2. Edit `/boot/firmware/config.txt` and change the `dtoverlay` line
    3. Swap the physical camera
    4. Power on and reboot
    
    The code automatically detects which camera is connected and configures the correct sensor dimensions (IMX708: 4608×2592, IMX219: 3280×2464).

### GMSL2 Camera Setup (Jetson + VL-GM2-8CAM-RPI22)

*   **Cameras not appearing as /dev/videoX?**
    1. Confirm Technexion kernel module is installed for your JetPack version
    2. Check dmesg: `dmesg | grep -iE "gmsl|max96|video"`
    3. Verify both CSI flex cables are seated firmly on both the Jetson and the VL-GM2-8CAM-RPI22 board
    4. List devices: `v4l2-ctl --list-devices`

*   **Wrong pixel format?**
    ```bash
    # Find what your driver reports:
    v4l2-ctl -d /dev/video0 --list-formats-ext
    # Then set the format env var:
    CAM_FORMAT=YUY2 atn-jetson-gmsl-webrtc
    ```

*   **nvv4l2h264enc not found?**
    ```bash
    # Install JetPack multimedia packages:
    sudo apt install nvidia-l4t-multimedia gstreamer1.0-plugins-bad
    # Verify:
    gst-inspect-1.0 nvv4l2h264enc
    ```

*   **webrtcbin not found / `gir1.2-gst-plugins-bad-1.0` missing?**
    ```bash
    sudo apt install gir1.2-gst-plugins-bad-1.0
    gst-inspect-1.0 webrtcbin
    ```

*   **4 cameras but Orin Nano Super is struggling (high CPU/GPU)?**
    Reduce stream resolution — the GPU-compute encoder path is the bottleneck:
    ```bash
    atn-jetson-gmsl-webrtc --cameras 4 --width 960 --height 540 --bitrate 2000000
    ```
    For true hardware NVENC at 4× 1080p30, upgrade to Jetson Orin NX 8 GB.

*   **Black/frozen video on one camera?**
    Test each camera independently with GStreamer:
    ```bash
    gst-launch-1.0 v4l2src device=/dev/video2 ! \
      video/x-raw,format=UYVY,width=1920,height=1080,framerate=30/1 ! \
      nvvidconv ! autovideosink
    ```

### General Issues

*   **Service fails to start?**
    Check logs: `sudo journalctl -u <service_name> -n 50`
*   **Camera not found?**
    *   Pi: Run `libcamera-hello --list-cameras` or `vcgencmd get_camera`
    *   Jetson (GMSL2): Run `v4l2-ctl --list-devices`
    *   Jetson (legacy): Check `nvargus-daemon` status
*   **Permission denied?**
    Ensure the user `atn` (or your configured user) has access to video devices (`sudo usermod -aG video atn`)
*   **Black screen on WebRTC connection?**
    *   Wait up to 1 second for first keyframe (SPS/PPS headers)
    *   Check browser console for WebRTC errors
    *   Verify webrtcbin ICE candidates: look for `ICE connection state: connected` in server logs
    *   On direct Ethernet with no internet: STUN gathering will time out (normal) — host candidates are used

## 📂 Project Structure

```
atn_picam/
├── src/atn_picam/
│   ├── core/           # Shared utilities (storage management)
│   ├── jetson/         # Jetson-specific logic
│   │   ├── gmsl_webrtc.py  # GMSL2 4-camera WebRTC (gst-webrtcbin) ← PRODUCTION
│   │   ├── webrtc.py       # Single-camera WebRTC (aiortc, legacy)
│   │   └── stream.py       # MJPEG streaming (legacy)
│   ├── pizero/         # Pi-specific logic
│   │   ├── webrtc.py   # WebRTC streaming
│   │   └── stream.py   # MJPEG streaming
│   └── templates/      # HTML templates for web interfaces
│       ├── gmsl_webrtc.html    # 4-camera 2×2 grid UI ← PRODUCTION
│       └── ...                 # Legacy single-camera UIs
├── scripts/            # Systemd services and setup scripts
└── pyproject.toml      # Package configuration
```

## 🎯 Performance Optimization Details

### Jetson WebRTC Hardware Acceleration

The Jetson WebRTC implementation uses **dual hardware H.264 encoders** for maximum efficiency:

| Component | Configuration | Impact |
|-----------|--------------|--------|
| **WebRTC Encoder** | 960×540 @ 3Mbps, maxperf=1 | Primary stream, always active |
| **Recording Encoder** | 1920×1080 @ 8Mbps, maxperf=1 | On-demand, high quality |
| **Memory** | NVMM zero-copy | Eliminates CPU copies |
| **Packet Size** | ~20-30 KB (H.264) vs 778 KB (raw) | **25x reduction** |
| **CPU Usage** | 10-15% vs 15-25% (raw frames) | **~40% reduction** |

**Test Results:**
- Dual H.264 encoding verified on Jetson Orin Nano (JetPack 6.x)
- Both encoders run concurrently without resource exhaustion
- EMC bandwidth allocation: 376000 (WebRTC) + 846000 (Recording)

## 📋 Command Reference

### Console Commands
```bash
# Jetson — GMSL2 (production, 4-camera)
atn-jetson-gmsl-webrtc                      # 4 cams, 1280×720, 4 Mbps/cam (default)
atn-jetson-gmsl-webrtc --cameras 2          # only 2 cameras
atn-jetson-gmsl-webrtc --width 960 --height 540 --bitrate 2000000  # lower res for Nano Super

# Jetson — legacy single-camera
atn-jetson-webrtc          # single cam WebRTC (nvarguscamerasrc, aiortc)
atn-jetson-stream          # single cam MJPEG

# Raspberry Pi
atn-pizero-webrtc          # WebRTC streaming
atn-pizero-stream          # MJPEG streaming
```

### Service Management
```bash
# Status
sudo systemctl status jetson_webrtc.service

# Start/Stop
sudo systemctl start jetson_webrtc.service
sudo systemctl stop jetson_webrtc.service

# Enable/Disable auto-start
sudo systemctl enable jetson_webrtc.service
sudo systemctl disable jetson_webrtc.service

# View logs
sudo journalctl -u jetson_webrtc.service -f
```
