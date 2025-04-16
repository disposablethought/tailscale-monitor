#!/bin/bash

# Tailscale Monitor Bot - Service Installation Script
# This script installs the Tailscale Monitor Bot as a systemd service

# Ensure script is run as root
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root" >&2
    echo "Try: sudo bash $0"
    exit 1
fi

# Configuration (edit these variables)
BOT_USER="$(whoami)"
BOT_DIR="$(pwd)"
VENV_PATH="$BOT_DIR/venv"
BOT_SCRIPT="$BOT_DIR/bot.py"
SERVICE_NAME="tailscale-monitor"
DISCORD_TOKEN="${DISCORD_BOT_TOKEN:-}"

# Ask for Discord token if not set
if [ -z "$DISCORD_TOKEN" ]; then
    read -p "Enter your Discord bot token: " DISCORD_TOKEN
fi

echo "=== Installing Tailscale Monitor Bot Service ==="
echo "User: $BOT_USER"
echo "Directory: $BOT_DIR"
echo "Python: $VENV_PATH/bin/python"
echo "Bot script: $BOT_SCRIPT"

# Create the service file
cat > /etc/systemd/system/$SERVICE_NAME.service << EOF
[Unit]
Description=Tailscale Monitor Discord Bot
After=network.target

[Service]
Type=simple
User=$BOT_USER
WorkingDirectory=$BOT_DIR
ExecStart=$VENV_PATH/bin/python $BOT_SCRIPT
Restart=on-failure
RestartSec=10
Environment="DISCORD_BOT_TOKEN=$DISCORD_TOKEN"

# Hardening options
ProtectSystem=full
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

# Set proper permissions
chmod 644 /etc/systemd/system/$SERVICE_NAME.service

# Reload systemd
systemctl daemon-reload

# Enable and start the service
systemctl enable $SERVICE_NAME
systemctl start $SERVICE_NAME

# Check status
echo ""
echo "=== Service Status ==="
systemctl status $SERVICE_NAME

echo ""
echo "=== Installation Complete ==="
echo "Your bot has been installed as a systemd service!"
echo ""
echo "Useful commands:"
echo "  Check status: sudo systemctl status $SERVICE_NAME"
echo "  View logs: sudo journalctl -u $SERVICE_NAME -f"
echo "  Restart service: sudo systemctl restart $SERVICE_NAME"
echo "  Stop service: sudo systemctl stop $SERVICE_NAME"
