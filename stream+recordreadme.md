# Pi Camera Stream + Record Server

Simultaneously records high-quality H.264 video to local storage while providing a web preview stream. Optimized for Raspberry Pi Zero 2 W and Camera Module 3.

## What This Does

1. **Records** 1920×1080 @ 30fps H.264 video to local files (15Mbps, ~6.75 GB/hour)
2. **Streams** 1280×720 MJPEG to web browser for live monitoring (10Mbps)
3. Uses hardware encoders only - runs at 40-55% CPU on Pi Zero 2 W

## How It Works

### Data Flow

```
Camera Sensor (12MP)
    ↓
ISP splits into 2 streams:
    ↓
    ├─→ Main (1920×1080)  →  H.264 Encoder  →  recordings/drone_*.h264
    └─→ Lores (1280×720)  →  MJPEG Encoder  →  Web browser :8080
```

### Hardware Usage

| Component | Used | Purpose |
|-----------|------|---------|
| **Camera Module 3 sensor** | 1× | Captures 12MP (4608×2592) raw image |
| **ISP (Image Signal Processor)** | 1× | Processes raw image, outputs 2 streams (main + lores) |
| **H.264 hardware encoder** | 1× | Encodes main stream to file |
| **MJPEG hardware encoder** | 1× | Encodes lores stream for web |

**Key point:** Each hardware component is used only once. The ISP creates two streams from a single sensor capture, then each stream gets its own hardware encoder.

## Quick Start

```bash
# Install dependencies
sudo apt install python3-picamera2 python3-flask python3-psutil

# Run
python3 camera_stream_record.py

# Access web preview
http://<pi-ip>:8080
```

Recordings save to `recordings/` as timestamped H.264 files.

## Configuration

**Change recording quality** (in `start_recording()`):
```python
h264_encoder = H264Encoder(bitrate=15000000)  # 15Mbps
```

**Change resolutions** (in `init_camera()`):
```python
main={"size": (1920, 1080)}  # Recording resolution
lores={"size": (1280, 720)}  # Web preview resolution
```

**Change frame rate** (in `init_camera()`):
```python
"FrameRate": 30.0  # FPS for recording
```

## Convert H.264 to MP4

```bash
ffmpeg -i recordings/drone_20241111_143022.h264 -c:v copy output.mp4
```

## Performance

- **CPU:** 40-55%
- **RAM:** 200-300 MB
- **Storage:** 1.875 MB/s write speed
- **Network:** 10Mbps per web client

## Troubleshooting

**Camera not detected:**
```bash
libcamera-hello --list-cameras
```

**High CPU:** Reduce bitrate or resolution

**Low storage:** Recordings use ~6.75 GB/hour at default settings

## Why This Is Efficient

- All encoding is hardware-accelerated (no CPU encoding)
- H.264 writes directly to file (no transcoding)
- ISP handles all image processing and downscaling
- Two independent encoders = no bottlenecks