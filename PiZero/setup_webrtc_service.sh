#!/bin/bash

SERVICE_NAME="picam_webrtc.service"
SERVICE_FILE="picam_webrtc.service"
SYSTEMD_PATH="/etc/systemd/system/$SERVICE_NAME"

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
  echo "Please run as root (sudo ./setup_webrtc_service.sh)"
  exit
fi

echo "Setting up $SERVICE_NAME..."

# Stop existing service if running
if systemctl is-active --quiet $SERVICE_NAME; then
    echo "Stopping existing service..."
    systemctl stop $SERVICE_NAME
fi

# Copy service file
echo "Copying service file to $SYSTEMD_PATH..."
cp $SERVICE_FILE $SYSTEMD_PATH

# Set permissions
chmod 644 $SYSTEMD_PATH

# Reload systemd daemon
echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enable service
echo "Enabling service..."
systemctl enable $SERVICE_NAME

# Start service
echo "Starting service..."
systemctl start $SERVICE_NAME

# Show status
echo "Service status:"
systemctl status $SERVICE_NAME --no-pager
