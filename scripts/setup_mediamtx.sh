#!/bin/bash

# Setup script for mediamtx on Raspberry Pi Zero 2 W
# Installs mediamtx and configures it for hardware-accelerated streaming

MEDIAMTX_VERSION="v1.9.3"
ARCH="linux_arm64v8"
INSTALL_DIR="/usr/local/bin"
CONFIG_FILE="$INSTALL_DIR/mediamtx.yml"

# Check for root
if [ "$EUID" -ne 0 ]; then 
  echo "Please run as root (sudo ./setup_mediamtx.sh)"
  exit
fi

echo "Installing mediamtx $MEDIAMTX_VERSION..."

# Download
wget "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_${ARCH}.tar.gz" -O mediamtx.tar.gz

# Unpack
tar -xzf mediamtx.tar.gz

# Install
mv mediamtx "$INSTALL_DIR/"
mv mediamtx.yml "$CONFIG_FILE"

# Cleanup
rm mediamtx.tar.gz LICENSE

# Configure mediamtx.yml
echo "Configuring $CONFIG_FILE..."

# Backup original config if it exists and isn't already backed up
if [ -f "$CONFIG_FILE" ] && [ ! -f "$CONFIG_FILE.bak" ]; then
    mv "$CONFIG_FILE" "$CONFIG_FILE.bak"
    echo "Backed up original config to $CONFIG_FILE.bak"
fi

# Write a clean, minimal configuration file
# We do NOT use runOnInit here because the Python script (webrtc_hardware.py)
# manages the ffmpeg process and pushes the stream to mediamtx.
cat <<EOF > "$CONFIG_FILE"
###############################################
# MediaMTX Configuration for ATN PiCam
###############################################

paths:
  # Configuration for the 'cam' path
  cam:
    # Stream is pushed by Python script via RTSP
    source: publisher
    
  # Default configuration for all other paths
  all_others:
EOF

echo "Installation complete!"
echo "To start the server: $INSTALL_DIR/mediamtx $CONFIG_FILE"
echo "Stream URL: http://<pi-ip>:8889/cam"
