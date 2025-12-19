#!/bin/bash
#
# RSCP Installation Script
# For Debian/Ubuntu-based systems (including LXC containers)
#
# Usage: curl -sSL https://raw.githubusercontent.com/gcbtech/RSCP/main/install.sh | bash
#    or: wget -qO- https://raw.githubusercontent.com/gcbtech/RSCP/main/install.sh | bash
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

INSTALL_DIR="/opt/rscp"
SERVICE_USER="rscp"

echo -e "${BLUE}"
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║              RSCP Installation Script                     ║"
echo "║         Receive, Scan, Check, Process                     ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: Please run as root (sudo)${NC}"
    exit 1
fi

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
    VERSION=$VERSION_ID
else
    echo -e "${RED}Error: Cannot detect OS. This script requires Debian or Ubuntu.${NC}"
    exit 1
fi

if [[ "$OS" != "debian" && "$OS" != "ubuntu" ]]; then
    echo -e "${YELLOW}Warning: This script is designed for Debian/Ubuntu.${NC}"
    echo -e "${YELLOW}Detected: $OS $VERSION${NC}"
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo -e "${GREEN}[1/6]${NC} Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

echo -e "${GREEN}[2/6]${NC} Installing dependencies..."
apt-get install -y -qq python3 python3-pip python3-venv git curl

echo -e "${GREEN}[3/6]${NC} Creating RSCP user and directory..."
# Create service user if it doesn't exist
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /bin/false "$SERVICE_USER"
fi

# Create install directory
mkdir -p "$INSTALL_DIR"

echo -e "${GREEN}[4/6]${NC} Downloading RSCP..."
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  Existing installation found, pulling updates..."
    cd "$INSTALL_DIR"
    git pull
else
    git clone https://github.com/gcbtech/RSCP.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

echo -e "${GREEN}[5/6]${NC} Installing Python dependencies..."
# Create virtual environment
python3 -m venv "$INSTALL_DIR/venv"
source "$INSTALL_DIR/venv/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q
deactivate

# Set ownership
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo -e "${GREEN}[6/6]${NC} Setting up systemd service..."
cat > /etc/systemd/system/rscp.service << EOF
[Unit]
Description=RSCP - Receiving Station Control Panel
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$INSTALL_DIR/venv/bin"
ExecStart=$INSTALL_DIR/venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rscp

echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  RSCP Installation Complete!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BLUE}Installation directory:${NC} $INSTALL_DIR"
echo -e "  ${BLUE}Service user:${NC} $SERVICE_USER"
echo ""
echo -e "  ${YELLOW}To start RSCP:${NC}"
echo "    sudo systemctl start rscp"
echo ""
echo -e "  ${YELLOW}To check status:${NC}"
echo "    sudo systemctl status rscp"
echo ""
echo -e "  ${YELLOW}To view logs:${NC}"
echo "    sudo journalctl -u rscp -f"
echo ""
echo -e "  ${YELLOW}Access RSCP at:${NC}"

# Try to get IP address
IP=$(hostname -I | awk '{print $1}')
if [ -n "$IP" ]; then
    echo -e "    ${GREEN}http://$IP:5000${NC}"
else
    echo "    http://<your-server-ip>:5000"
fi

echo ""
echo -e "  ${BLUE}First-time setup:${NC}"
echo "    1. Start the service: sudo systemctl start rscp"
echo "    2. Open the URL above in your browser"
echo "    3. Complete the setup wizard"
echo ""
echo -e "${GREEN}Thank you for installing RSCP!${NC}"
echo ""
