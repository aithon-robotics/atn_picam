# Camera Stream Auto-Start Setup Guide

This guide explains how to set up the camera streaming services to automatically start when the Pi Zero or Jetson boots up.

## Prerequisites

- Python script should be deployed to the device
- All dependencies installed (Flask, picamera2/GStreamer, etc.)
- User `atn` should have permissions to access camera

## Setup Instructions

### For Pi Zero

1. **Deploy files to Pi Zero:**
   ```bash
   # From your development machine
   cd /home/timathis/aithon_code/atn_picam
   scp PiZero/picam_stream.service atn@<pi-ip>:/home/atn/aithon_code/atn_picam/PiZero/
   scp PiZero/setup_autostart.sh atn@<pi-ip>:/home/atn/aithon_code/atn_picam/PiZero/
   ```

2. **SSH into Pi Zero:**
   ```bash
   ssh atn@<pi-ip>
   ```

3. **Run the setup script:**
   ```bash
   cd /home/atn/aithon_code/atn_picam/PiZero
   chmod +x setup_autostart.sh
   ./setup_autostart.sh
   ```

4. **Verify it's running:**
   ```bash
   sudo systemctl status picam_stream
   ```

5. **Test auto-start:**
   ```bash
   sudo reboot
   # Wait for Pi to reboot, then check if service started
   sudo systemctl status picam_stream
   ```

### For Jetson

1. **Deploy files to Jetson:**
   ```bash
   # From your development machine
   cd /home/timathis/aithon_code/atn_picam
   scp jetson/jetson_stream.service atn@<jetson-ip>:/home/atn/aithon_code/atn_picam/jetson/
   scp jetson/setup_autostart.sh atn@<jetson-ip>:/home/atn/aithon_code/atn_picam/jetson/
   ```

2. **SSH into Jetson:**
   ```bash
   ssh atn@<jetson-ip>
   ```

3. **Run the setup script:**
   ```bash
   cd /home/atn/aithon_code/atn_picam/jetson
   chmod +x setup_autostart.sh
   ./setup_autostart.sh
   ```

4. **Verify it's running:**
   ```bash
   sudo systemctl status jetson_stream
   ```

5. **Test auto-start:**
   ```bash
   sudo reboot
   # Wait for Jetson to reboot, then check if service started
   sudo systemctl status jetson_stream
   ```

## Service Management Commands

### Pi Zero Service

```bash
# Check service status
sudo systemctl status picam_stream

# Start the service
sudo systemctl start picam_stream

# Stop the service
sudo systemctl stop picam_stream

# Restart the service
sudo systemctl restart picam_stream

# Enable auto-start on boot
sudo systemctl enable picam_stream

# Disable auto-start on boot
sudo systemctl disable picam_stream

# View service logs (live)
sudo journalctl -u picam_stream -f

# View service logs (last 50 lines)
sudo journalctl -u picam_stream -n 50
```

### Jetson Service

Replace `picam_stream` with `jetson_stream` in the commands above.

## Troubleshooting

### Service fails to start

1. **Check logs:**
   ```bash
   sudo journalctl -u picam_stream -n 100
   # or
   sudo journalctl -u jetson_stream -n 100
   ```

2. **Check if script runs manually:**
   ```bash
   cd /home/atn/aithon_code/atn_picam/PiZero
   python3 picam_stream_ondemand_record.py
   # or for Jetson
   cd /home/atn/aithon_code/atn_picam/jetson
   python3 jetson_stream_ondemand_record.py
   ```

3. **Check permissions:**
   ```bash
   ls -la /home/atn/aithon_code/atn_picam/PiZero/picam_stream_ondemand_record.py
   # Make sure user 'atn' can read/execute
   ```

4. **Check if camera is accessible:**
   ```bash
   # For Pi Zero
   libcamera-hello --list-cameras
   
   # For Jetson
   v4l2-ctl --list-devices
   ```

### Service starts but crashes

- Check if all Python dependencies are installed
- Verify camera hardware is connected properly
- Check system logs: `dmesg | tail -50`

### Service doesn't auto-start after reboot

1. **Verify service is enabled:**
   ```bash
   sudo systemctl is-enabled picam_stream  # or jetson_stream
   ```

2. **Check service unit file:**
   ```bash
   sudo systemctl cat picam_stream  # or jetson_stream
   ```

## Accessing Recordings

Even with auto-start enabled, you can still access the Pi/Jetson to download recordings:

```bash
# SSH into the device
ssh atn@<device-ip>

# Recordings are stored in ~/recordings
cd ~/recordings
ls -lh

# Download recordings to your local machine
# From your development machine:
scp atn@<device-ip>:~/recordings/*.mp4 ./local_backup/
```

Or use the web interface to manage recordings remotely if you add a file browser endpoint.

## Removing Auto-Start

If you want to disable auto-start:

```bash
# Stop and disable the service
sudo systemctl stop picam_stream  # or jetson_stream
sudo systemctl disable picam_stream  # or jetson_stream

# Optional: Remove the service file
sudo rm /etc/systemd/system/picam_stream.service  # or jetson_stream.service
sudo systemctl daemon-reload
```

## Notes

- The service will automatically restart if it crashes (RestartSec=10)
- Logs are stored in systemd journal (use `journalctl` to view)
- The service starts after network is available
- Port 8080 must be accessible on the network for web interface
