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
├── jetson/                          # Jetson Orin Nano implementation
│   ├── jetson_stream_record.py     # Main application
│   ├── test_setup.py               # Hardware verification
│   ├── README.md                   # Full documentation
│   ├── QUICKSTART.md              # Quick start guide
│   ├── SUMMARY.md                 # Implementation summary
│   ├── IMPLEMENTATION_NOTES.md    # Technical deep dive
│   └── requirements.txt           # Python dependencies
│
└── PiZero/                         # Raspberry Pi implementation
    ├── picam_streamrecording.py   # Main application
    ├── stream+recordreadme.md     # Documentation
    └── ...
```

## 🚀 Quick Start

### For Jetson Orin Nano
```bash
cd jetson/
python3 test_setup.py          # Verify hardware
python3 jetson_stream_record.py  # Start recording
```
See [jetson/QUICKSTART.md](jetson/QUICKSTART.md) for details.

### For Raspberry Pi
```bash
cd PiZero/
python3 picam_streamrecording.py
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

- **Jetson Implementation:** See [jetson/README.md](jetson/README.md)
- **Pi Implementation:** See `PiZero/stream+recordreadme.md`