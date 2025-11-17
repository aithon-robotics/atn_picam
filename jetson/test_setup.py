#!/usr/bin/env python3
"""
Test script to verify Jetson camera and GStreamer setup
Checks all required components before running the main application
"""

import sys
import subprocess
import os

def print_status(check_name, success, details=""):
    """Print formatted status message"""
    status = "✓" if success else "✗"
    color = "\033[92m" if success else "\033[91m"
    reset = "\033[0m"
    print(f"{color}{status}{reset} {check_name}")
    if details:
        print(f"  {details}")
    return success

def check_python_imports():
    """Check if required Python packages are installed"""
    print("\n=== Python Dependencies ===")
    all_ok = True
    
    try:
        import flask
        print_status("Flask", True, f"version {flask.__version__}")
    except ImportError:
        print_status("Flask", False, "Run: pip3 install flask")
        all_ok = False
    
    try:
        import psutil
        print_status("psutil", True, f"version {psutil.__version__}")
    except ImportError:
        print_status("psutil", False, "Run: pip3 install psutil")
        all_ok = False
    
    try:
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        print_status("GStreamer Python bindings", True, f"GStreamer {Gst.version_string()}")
    except (ImportError, ValueError) as e:
        print_status("GStreamer Python bindings", False, 
                    "Run: sudo apt install python3-gi gir1.2-gstreamer-1.0")
        all_ok = False
    
    return all_ok

def check_camera_device():
    """Check if camera device exists"""
    print("\n=== Camera Device ===")
    
    if os.path.exists("/dev/video0"):
        # Get device info
        try:
            result = subprocess.run(
                ["ls", "-l", "/dev/video0"],
                capture_output=True, text=True, timeout=2
            )
            print_status("Camera device /dev/video0", True, result.stdout.strip())
            return True
        except Exception as e:
            print_status("Camera device /dev/video0", True, "exists but could not get details")
            return True
    else:
        print_status("Camera device /dev/video0", False, 
                    "Camera not detected. Check CSI connection.")
        return False

def check_gstreamer_plugins():
    """Check if required GStreamer plugins are available"""
    print("\n=== GStreamer Plugins ===")
    all_ok = True
    
    required_plugins = [
        ("nvarguscamerasrc", "NVIDIA ARGUS Camera Source (CSI)"),
        ("nvv4l2h264enc", "NVIDIA H.264 Hardware Encoder"),
        ("nvjpegenc", "NVIDIA JPEG Hardware Encoder"),
        ("nvvidconv", "NVIDIA Video Converter"),
    ]
    
    for plugin, description in required_plugins:
        try:
            result = subprocess.run(
                ["gst-inspect-1.0", plugin],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                print_status(f"{plugin}", True, description)
            else:
                print_status(f"{plugin}", False, f"Plugin not found. Check JetPack installation.")
                all_ok = False
        except Exception as e:
            print_status(f"{plugin}", False, f"Error: {e}")
            all_ok = False
    
    return all_ok

def test_camera_capture():
    """Test basic camera capture with GStreamer"""
    print("\n=== Camera Capture Test ===")
    
    print("Testing 2-second camera capture...")
    try:
        result = subprocess.run(
            [
                "gst-launch-1.0",
                "nvarguscamerasrc", "sensor-id=0", "num-buffers=60", "!",
                "video/x-raw(memory:NVMM), width=1920, height=1080, framerate=30/1", "!",
                "fakesink"
            ],
            capture_output=True, text=True, timeout=5
        )
        
        if result.returncode == 0:
            print_status("Camera capture", True, "Successfully captured 60 frames")
            return True
        else:
            print_status("Camera capture", False, f"Failed: {result.stderr[:200]}")
            return False
    except subprocess.TimeoutExpired:
        print_status("Camera capture", False, "Timeout - check camera connection")
        return False
    except Exception as e:
        print_status("Camera capture", False, f"Error: {e}")
        return False

def test_h264_encoder():
    """Test H.264 hardware encoder"""
    print("\n=== H.264 Encoder Test ===")
    
    print("Testing H.264 hardware encoding...")
    try:
        result = subprocess.run(
            [
                "gst-launch-1.0",
                "nvarguscamerasrc", "sensor-id=0", "num-buffers=30", "!",
                "video/x-raw(memory:NVMM), width=1920, height=1080, framerate=30/1", "!",
                "nvv4l2h264enc", "bitrate=5000000", "!",
                "fakesink"
            ],
            capture_output=True, text=True, timeout=5
        )
        
        if result.returncode == 0:
            print_status("H.264 encoding", True, "NVENC working")
            return True
        else:
            print_status("H.264 encoding", False, f"Failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        print_status("H.264 encoding", False, f"Error: {e}")
        return False

def test_jpeg_encoder():
    """Test JPEG hardware encoder"""
    print("\n=== JPEG Encoder Test ===")
    
    print("Testing JPEG hardware encoding...")
    try:
        result = subprocess.run(
            [
                "gst-launch-1.0",
                "nvarguscamerasrc", "sensor-id=0", "num-buffers=10", "!",
                "video/x-raw(memory:NVMM), width=1920, height=1080, framerate=30/1", "!",
                "nvvidconv", "!",
                "video/x-raw, format=I420", "!",
                "nvjpegenc", "!",
                "fakesink"
            ],
            capture_output=True, text=True, timeout=10
        )
        
        if result.returncode == 0:
            print_status("JPEG encoding", True, "NVJPEG working")
            return True
        else:
            print_status("JPEG encoding", False, f"Failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        print_status("JPEG encoding", False, f"Error: {e}")
        return False

def main():
    """Run all checks"""
    print("=" * 70)
    print("Jetson Camera Setup Verification")
    print("=" * 70)
    
    results = []
    
    # Run checks
    results.append(("Python Dependencies", check_python_imports()))
    results.append(("Camera Device", check_camera_device()))
    results.append(("GStreamer Plugins", check_gstreamer_plugins()))
    results.append(("Camera Capture", test_camera_capture()))
    results.append(("H.264 Encoder", test_h264_encoder()))
    results.append(("JPEG Encoder", test_jpeg_encoder()))
    
    # Summary
    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    
    passed = sum(1 for _, success in results if success)
    total = len(results)
    
    for name, success in results:
        status = "PASS" if success else "FAIL"
        color = "\033[92m" if success else "\033[91m"
        reset = "\033[0m"
        print(f"{name:.<40} {color}{status}{reset}")
    
    print("=" * 70)
    print(f"Results: {passed}/{total} checks passed")
    
    if passed == total:
        print("\n✓ All checks passed! You can run jetson_stream_record.py")
        return 0
    else:
        print("\n✗ Some checks failed. Please fix the issues above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
