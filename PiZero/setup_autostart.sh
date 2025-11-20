#!/bin/bash
# Script to set up auto-start for Pi Camera Stream service
# Run this script on the Pi Zero

echo "Setting up Pi Camera Stream service for auto-start..."

# Copy service file to systemd directory
sudo cp picam_stream.service /etc/systemd/system/

# Reload systemd to recognize the new service
sudo systemctl daemon-reload

# Enable the service to start on boot
sudo systemctl enable picam_stream.service

# Start the service now
sudo systemctl start picam_stream.service

# Check status
echo ""
echo "Service setup complete!"
echo "Checking service status..."
sudo systemctl status picam_stream.service

echo ""
echo "Useful commands:"
echo "  sudo systemctl status picam_stream    # Check if service is running"
echo "  sudo systemctl stop picam_stream      # Stop the service"
echo "  sudo systemctl start picam_stream     # Start the service"
echo "  sudo systemctl restart picam_stream   # Restart the service"
echo "  sudo systemctl disable picam_stream   # Disable auto-start"
echo "  sudo journalctl -u picam_stream -f    # View live logs"
