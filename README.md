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
| **WebRTC** | ✅ Low-latency streaming | ❌ (Planned) |
| **Hardware Accel** | ✅ VideoCore ISP + Encoder | ✅ NVENC + NVJPEG + NVMM |
| **CPU Usage** | ~40-55% (Pi Zero 2 W) | ~15-25% |
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

### 🟢 NVIDIA Jetson

*   **Stream + Record:**
    ```bash
    atn-jetson-stream
    ```
    *   Access at `http://<jetson-ip>:8080`
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
*   **Library:** GStreamer (via Python bindings)
*   **Pipeline:** `nvarguscamerasrc` -> `tee` split:
    1.  **Branch 1:** `nvv4l2h264enc` (Hardware H.264) -> `.mp4` file.
    2.  **Branch 2:** `nvjpegenc` (Hardware JPEG) -> MJPEG web stream.
*   **Optimization:** Uses Zero-Copy memory (NVMM) to minimize CPU usage.

## 🛠 Troubleshooting

*   **Service fails to start?**
    Check logs: `sudo journalctl -u <service_name> -n 50`
*   **Camera not found?**
    *   Pi: Run `libcamera-hello --list-cameras`
    *   Jetson: Run `v4l2-ctl --list-devices`
*   **Permission denied?**
    Ensure the user `atn` (or your configured user) has access to video devices (`sudo usermod -aG video atn`).

## 📂 Project Structure

```
atn_picam/
├── src/atn_picam/
│   ├── core/           # Shared utilities
│   ├── jetson/         # Jetson-specific logic
│   ├── pizero/         # Pi-specific logic
│   └── templates/      # HTML templates for web interfaces
├── scripts/            # Systemd services and setup scripts
└── pyproject.toml      # Package configuration
```
