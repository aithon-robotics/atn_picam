# Camera Stream + Record System

Multi-platform camera recording and streaming system optimized for different hardware platforms.

## 🎯 Overview

This project provides simultaneous high-quality video recording and web streaming for:
- **Raspberry Pi** with Camera Module (picamera2)
- **NVIDIA Jetson** with CSI Camera (GStreamer + NVENC)

Both implementations record H.264 video to disk while streaming MJPEG to a web browser.

## 📁 Project Structure

```
atn_picam/
├── src/
│   └── atn_picam/
│       ├── core/           # Shared logic
│       ├── jetson/         # Jetson Orin Nano implementation
│       └── pizero/         # Raspberry Pi implementation
├── scripts/                # Service files and setup scripts
├── docs/                   # Documentation
└── pyproject.toml          # Package configuration
```

## 🚀 Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/aithon-robotics/atn_picam.git
   cd atn_picam
   ```

2. Install the package:
   ```bash
   pip install -e .
   ```

## 🚀 Usage

### For Jetson Orin Nano
```bash
atn-jetson-stream
```
See [docs/JETSON_SUMMARY.md](docs/JETSON_SUMMARY.md) for details.

### For Raspberry Pi
Standard streaming + recording:
```bash
atn-pizero-stream
```

WebRTC streaming:
```bash
atn-pizero-webrtc
```

Immediate recording:
```bash
atn-pizero-record
```

## 🔧 Hardware Requirements

### Jetson Orin Nano
- NVIDIA Jetson Orin Nano Developer Kit
- Pi Camera Module v2 (8MP) via CSI connector
- JetPack 5.0+ installed

### Raspberry Pi
- Raspberry Pi (tested on Zero 2 W)
- Pi Camera Module v2 or v3 via CSI connector
- Raspberry Pi OS with picamera2

## 📊 Feature Comparison

| Feature | Raspberry Pi | Jetson Orin Nano |
|---------|-------------|------------------|
| **Recording** | 1920×1080 @ 30fps | 1920×1080 @ 30fps |
| **Streaming** | 1280×720 MJPEG | 1280×720 MJPEG |
| **API** | picamera2 | GStreamer |
| **H.264 Encoder** | Hardware | NVENC (hardware) |
| **Output Format** | .h264 | .mp4 |
| **Web Interface** | ✅ Port 8080 | ✅ Port 8080 |

## 🎨 Architecture

### Raspberry Pi
```
Camera → picamera2 → [H.264 encoder → file]
                  → [MJPEG encoder → web]
```

### Jetson Orin Nano
```
Camera → nvarguscamerasrc → tee → [nvv4l2h264enc → file]
                                → [nvjpegenc → web]
```

## 📖 Documentation

- **Jetson Implementation:** See [docs/JETSON_SUMMARY.md](docs/JETSON_SUMMARY.md)
- **Pi Implementation:** See `docs/stream+recordreadme.md`
