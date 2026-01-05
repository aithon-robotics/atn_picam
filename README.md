# ATN PiCam: Multi-Platform Camera Streaming & Recording

A unified Python package for high-performance camera streaming and recording on **NVIDIA Jetson** and **Raspberry Pi** devices.

## 🎯 Overview

This project provides simultaneous high-quality video recording and web streaming. It abstracts hardware differences to provide a consistent interface across platforms.

- **Raspberry Pi (Zero 2 W / 3 / 4 / 5)**: Uses `picamera2` and hardware encoders.
- **NVIDIA Jetson (Orin Nano / Nano)**: Uses GStreamer with NVENC/NVJPEG hardware acceleration.

## ✨ Features

| Feature | Raspberry Pi | Jetson Orin Nano |
|---------|-------------|------------------|
| **Recording** | 1920×1080 @ 30fps (.h264) | 1920×1080 @ 30fps (.mp4) |
| **Streaming** | 1280×720 MJPEG @ 15fps | 1280×720 MJPEG @ 15fps |
| **WebRTC** | ✅ Low-latency streaming | ✅ **Hardware-accelerated H.264** |
| **WebRTC Resolution** | Variable (browser negotiated) | 960×540 @ 3Mbps (H.264) |
| **Hardware Accel** | ✅ VideoCore ISP + Encoder | ✅ **Dual NVENC** + NVJPEG + NVMM |
| **CPU Usage** | ~40-55% (Pi Zero 2 W) | **~10-15%** (optimized) |
| **Web Interface** | ✅ Control & Preview | ✅ Control & Preview |

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

### 🟢 NVIDIA Jetson

*   **Stream + Record (MJPEG):**
    ```bash
    atn-jetson-stream
    ```
    *   Access at `http://<jetson-ip>:8080`
    *   Records to `~/recordings/`

*   **WebRTC Streaming (Low Latency, Hardware H.264):**
    ```bash
    atn-jetson-webrtc
    ```
    *   Access at `http://<jetson-ip>:8080`
    *   Hardware-accelerated H.264 encoding for WebRTC
    *   Dual encoder support for simultaneous streaming and recording
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

### NVIDIA Jetson

#### Stream Mode (MJPEG)
*   **Library:** GStreamer (via Python bindings)
*   **Pipeline:** `nvarguscamerasrc` -> `tee` split:
    1.  **Branch 1:** `nvv4l2h264enc` (Hardware H.264) -> `.mp4` file.
    2.  **Branch 2:** `nvjpegenc` (Hardware JPEG) -> MJPEG web stream.
*   **Optimization:** Uses Zero-Copy memory (NVMM) to minimize CPU usage.

#### WebRTC Mode (Hardware H.264 - Optimized)
*   **Library:** GStreamer + aiortc
*   **Pipeline:** `nvarguscamerasrc` -> `tee` split:
    1.  **WebRTC Branch (Always Active):**
        - `nvvidconv` (downscale to 960×540) -> `nvv4l2h264enc` (Hardware H.264, 3Mbps) -> WebRTC stream
    2.  **Recording Branch (On-Demand):**
        - `nvv4l2h264enc` (Hardware H.264, 8Mbps, 1920×1080) -> `.mp4` file
*   **Key Features:**
    - **Dual Hardware Encoders:** Both WebRTC and recording use independent `nvv4l2h264enc` instances
    - **Max Performance Mode:** `maxperf-enable=1` for optimal multi-encoder performance
    - **Zero-Copy Memory:** NVMM reduces CPU overhead
    - **SPS/PPS Insertion:** Automatic keyframe headers for reliable decoding
*   **CPU Usage:** ~10-15% (down from 15-25% with raw frame streaming)
*   **Latency:** <100ms with hardware encoding

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

### General Issues

*   **Service fails to start?**
    Check logs: `sudo journalctl -u <service_name> -n 50`
*   **Camera not found?**
    *   Pi: Run `libcamera-hello --list-cameras` or `vcgencmd get_camera`
    *   Jetson: Run `v4l2-ctl --list-devices` or check `nvargus-daemon` status
*   **Permission denied?**
    Ensure the user `atn` (or your configured user) has access to video devices (`sudo usermod -aG video atn`)
*   **WebRTC errors on Jetson?**
    *   Verify hardware encoder: `gst-inspect-1.0 nvv4l2h264enc`
    *   Check dual encoding test: Run `./test_dual_h264_encoding.sh` in project root
    *   Monitor CPU/memory: System stats displayed in console every 3 seconds
*   **Black screen on WebRTC connection?**
    *   Wait up to 1 second for first keyframe with SPS/PPS headers
    *   Check browser console for WebRTC errors
    *   Ensure firewall allows WebRTC ports (UDP/TCP)

## 📂 Project Structure

```
atn_picam/
├── src/atn_picam/
│   ├── core/           # Shared utilities (storage management)
│   ├── jetson/         # Jetson-specific logic
│   │   ├── webrtc.py   # Hardware H.264 WebRTC (optimized)
│   │   └── stream.py   # MJPEG streaming
│   ├── pizero/         # Pi-specific logic
│   │   ├── webrtc.py   # WebRTC streaming
│   │   └── stream.py   # MJPEG streaming
│   └── templates/      # HTML templates for web interfaces
├── scripts/            # Systemd services and setup scripts
├── test_dual_h264_encoding.sh  # Hardware encoder verification
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
# Jetson
atn-jetson-webrtc          # Hardware H.264 WebRTC (recommended)
atn-jetson-stream          # MJPEG streaming

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
