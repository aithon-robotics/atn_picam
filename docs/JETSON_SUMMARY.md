# Jetson Orin Nano Camera Implementation - Summary

## ✅ Implementation Complete

Successfully ported Raspberry Pi camera stream + record system to NVIDIA Jetson Orin Nano with hardware acceleration.

## 📁 Files Created

```
jetson/
├── jetson_stream_record.py      # Main application (executable)
├── test_setup.py                # Setup verification script (executable)
├── requirements.txt             # Python dependencies
├── README.md                    # Full documentation
├── QUICKSTART.md               # Quick start guide
├── IMPLEMENTATION_NOTES.md     # Technical comparison Pi vs Jetson
└── recordings/                 # Auto-created for video files
```

## 🎯 What It Does

**Simultaneously:**
1. Records 1920×1080 @ 30fps H.264 video to MP4 files (15Mbps)
2. Streams 1280×720 @ 15fps MJPEG to web browser (port 8080)
3. Uses NVIDIA hardware acceleration (NVENC + NVJPEG)

**Expected Performance:**
- CPU: 15-25% (vs 40-55% on Pi Zero 2 W)
- RAM: 300-400 MB
- Storage: ~6.75 GB/hour

## 🚀 Quick Start

### 1. Verify Setup
```bash
cd /home/atn/packages/atn_picam/jetson
python3 test_setup.py
```

**Result:** ✅ All 6 checks passed!

### 2. Run Server
```bash
python3 jetson_stream_record.py
```

### 3. Access Web Interface
```
http://<jetson-ip>:8080
```

## 🔧 Key Implementation Details

### Hardware Architecture
```
Pi Camera v2 (CSI) → nvarguscamerasrc → tee
                                         ├─→ nvv4l2h264enc → MP4 file
                                         └─→ nvjpegenc → MJPEG web stream
```

### Technologies Used
- **GStreamer** - Media pipeline framework
- **nvarguscamerasrc** - NVIDIA ARGUS camera source (CSI cameras)
- **NVENC** - Hardware H.264 encoder (nvv4l2h264enc)
- **NVJPEG** - Hardware JPEG encoder (nvjpegenc)
- **NVMM** - Zero-copy memory management
- **Flask** - Web server for MJPEG streaming

### Critical Design Choices

1. **nvarguscamerasrc instead of nvv4l2camerasrc**
   - CSI cameras (like Pi Camera Module) require ARGUS API
   - nvv4l2camerasrc is for USB/V4L2 cameras

2. **tee element for stream splitting**
   - Single camera source split into two branches
   - Avoids "camera already in use" errors
   - Each branch has independent queue for non-blocking operation

3. **MP4 container instead of raw H.264**
   - More compatible with media players
   - Can be played directly in browsers
   - Includes metadata and timestamps

4. **JPEG → MJPEG conversion**
   - Jetson has JPEG encoder (not MJPEG)
   - Individual JPEG frames constructed into MJPEG in Python
   - More flexible for saving snapshots

## 📊 Comparison to Raspberry Pi

| Feature | Raspberry Pi Zero 2 W | Jetson Orin Nano |
|---------|----------------------|------------------|
| **API** | picamera2 (Python) | GStreamer (pipeline) |
| **CPU Usage** | 40-55% | 15-25% |
| **Camera Source** | libcamera | ARGUS (nvarguscamerasrc) |
| **H.264 Encoder** | Hardware | Hardware (NVENC) |
| **MJPEG** | Native hardware | Constructed from JPEG |
| **Memory** | Standard DMA | NVMM (zero-copy) |
| **Complexity** | Simpler | More complex |
| **Performance** | Good | Excellent |

## ✨ Advantages of Jetson Implementation

1. **Lower CPU usage** - Better hardware acceleration
2. **Zero-copy memory** - NVMM eliminates memory copies
3. **Professional features** - GStreamer ecosystem
4. **Better scalability** - Can add RTSP, multiple cameras, etc.
5. **CUDA integration** - Can add AI/ML processing
6. **Higher quality** - NVENC produces better quality at same bitrate

## 🧪 Testing Results

All hardware components verified:
- ✅ Python dependencies (Flask, psutil, GStreamer bindings)
- ✅ Camera device (/dev/video0)
- ✅ GStreamer NVIDIA plugins
- ✅ Camera capture (60 frames @ 1920×1080)
- ✅ H.264 hardware encoding (NVENC)
- ✅ JPEG hardware encoding (NVJPEG)

## 📖 Documentation

- **QUICKSTART.md** - Get started in 3 steps
- **README.md** - Complete documentation with configuration
- **IMPLEMENTATION_NOTES.md** - Technical deep dive comparing Pi vs Jetson

## 🔒 No Hardware Conflicts

Each hardware component used exactly once:
- 1× Camera sensor
- 1× nvarguscamerasrc (camera source)
- 2× nvvidconv (video converter - one per branch)
- 1× nvv4l2h264enc (H.264 encoder)
- 1× nvjpegenc (JPEG encoder)
- 1× tee (software element for splitting)

**Result:** No "device busy" or resource conflict errors!

## 🎓 Usage Examples

### Start Recording
```bash
python3 jetson_stream_record.py
```

### View Live Stream
Open browser to `http://<jetson-ip>:8080`

### Play Recorded Video
```bash
vlc recordings/jetson_20241117_143022.mp4
```

### Convert to Different Format
```bash
ffmpeg -i recordings/jetson_20241117_143022.mp4 output.avi
```

## 🔮 Future Enhancements

Possible additions:
- RTSP streaming (professional workflows)
- H.265/HEVC encoding (better compression)
- Multiple camera support
- TensorRT AI inference on stream
- CUDA-accelerated image processing
- Motion detection
- Cloud upload

## 📝 Notes

- Recordings saved as MP4 in `recordings/` directory
- Filename format: `jetson_YYYYMMDD_HHMMSS.mp4`
- Storage: ~1.875 MB/s (~6.75 GB/hour at 15Mbps)
- Web interface shows live preview while recording
- Press Ctrl+C to stop cleanly