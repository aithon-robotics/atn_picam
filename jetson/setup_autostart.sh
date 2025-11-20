#!/bin/bash
# Script to set up auto-start for Jetson Camera Stream service
# Run this script on the Jetson

echo "Setting up Jetson Camera Stream service for auto-start..."

# Copy service file to systemd directory
sudo cp jetson_stream.service /etc/systemd/system/

# Reload systemd to recognize the new service
sudo systemctl daemon-reload

# Enable the service to start on boot
sudo systemctl enable jetson_stream.service

# Start the service now
sudo systemctl start jetson_stream.service

# Check status
echo ""
echo "Service setup complete!"
echo "Checking service status..."
sudo systemctl status jetson_stream.service

echo ""
echo "Useful commands:"
echo "  sudo systemctl status jetson_stream    # Check if service is running"
echo "  sudo systemctl stop jetson_stream      # Stop the service"
echo "  sudo systemctl start jetson_stream     # Start the service"
echo "  sudo systemctl restart jetson_stream   # Restart the service"
echo "  sudo systemctl disable jetson_stream   # Disable auto-start"
echo "  sudo journalctl -u jetson_stream -f    # View live logs"
