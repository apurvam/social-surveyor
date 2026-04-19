#!/bin/bash
# social-surveyor EC2 bootstrap. Runs once on a fresh Ubuntu 24.04 instance.
# Safe to re-run (idempotent). Requires sudo and internet access.
# Assumes the repo has already been cloned/rsynced to $REPO_DIR.

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/social-surveyor}"
DATA_DIR="${DATA_DIR:-/var/lib/social-surveyor}"
SERVICE_USER="${SERVICE_USER:-social-surveyor}"

echo "==> social-surveyor bootstrap starting"
echo "    repo: $REPO_DIR"
echo "    data: $DATA_DIR"
echo "    user: $SERVICE_USER"

# --- System packages ---
echo "==> apt: updating and installing prerequisites"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    git \
    build-essential \
    libssl-dev \
    pkg-config \
    curl \
    ca-certificates \
    unzip

# --- AWS CLI v2 (Ubuntu 24.04 dropped the apt awscli package) ---
if ! command -v aws >/dev/null 2>&1; then
    echo "==> awscli: installing AWS CLI v2 for aarch64"
    curl -LsSf https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip -o /tmp/awscliv2.zip
    sudo unzip -q -o /tmp/awscliv2.zip -d /tmp/
    sudo /tmp/aws/install --update
    rm -rf /tmp/awscliv2.zip /tmp/aws
else
    echo "==> awscli: already installed at $(command -v aws)"
fi

# --- uv install (system-wide so both root and the service user can invoke it) ---
if ! command -v uv >/dev/null 2>&1; then
    echo "==> uv: installing to /usr/local/bin"
    curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
    sudo UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh
    rm -f /tmp/uv-install.sh
else
    echo "==> uv: already installed at $(command -v uv)"
fi

# --- Service user ---
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "==> useradd: creating $SERVICE_USER"
    sudo useradd --system --create-home --shell /bin/bash "$SERVICE_USER"
else
    echo "==> useradd: $SERVICE_USER already exists"
fi

# --- Directory structure ---
echo "==> mkdir: $REPO_DIR and $DATA_DIR"
sudo mkdir -p "$REPO_DIR" "$DATA_DIR"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR" "$DATA_DIR"

# --- Systemd unit (the repo must already be present at $REPO_DIR) ---
UNIT_SRC="$REPO_DIR/deploy/social-surveyor@.service"
if [ -f "$UNIT_SRC" ]; then
    echo "==> systemd: installing social-surveyor@.service"
    sudo cp "$UNIT_SRC" /etc/systemd/system/
    sudo systemctl daemon-reload
else
    echo "==> WARN: $UNIT_SRC not found."
    echo "    Clone the repo into $REPO_DIR first, then re-run this script to install the systemd unit."
fi

# --- SSM env loader (reads from SSM Parameter Store, writes systemd EnvironmentFile) ---
echo "==> installing /usr/local/bin/social-surveyor-load-env"
sudo tee /usr/local/bin/social-surveyor-load-env > /dev/null <<'LOADENV'
#!/bin/bash
# Pull secrets from SSM Parameter Store into a systemd EnvironmentFile.
# Usage: social-surveyor-load-env <project-name>
#        e.g. social-surveyor-load-env opendata

set -euo pipefail

PROJECT="${1:-opendata}"
PREFIX="/social-surveyor/${PROJECT}"
ENV_DIR="/etc/social-surveyor"
ENV_FILE="${ENV_DIR}/${PROJECT}.env"
SERVICE_USER="social-surveyor"

sudo mkdir -p "$ENV_DIR"
sudo chmod 750 "$ENV_DIR"
sudo chown "root:$SERVICE_USER" "$ENV_DIR"

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

aws ssm get-parameters-by-path \
    --path "${PREFIX}" \
    --with-decryption \
    --query "Parameters[*].[Name,Value]" \
    --output text | \
while IFS=$'\t' read -r name value; do
    [ -z "$name" ] && continue
    key=$(basename "$name")
    printf '%s=%s\n' "$key" "$value"
done > "$TMP"

if ! [ -s "$TMP" ]; then
    echo "ERROR: no parameters found under $PREFIX. Did you run seed-ssm.sh?" >&2
    exit 1
fi

sudo install -m 640 -o root -g "$SERVICE_USER" "$TMP" "$ENV_FILE"
echo "wrote $ENV_FILE ($(wc -l < "$ENV_FILE") parameters)"
LOADENV
sudo chmod +x /usr/local/bin/social-surveyor-load-env

echo ""
echo "==> bootstrap complete"
echo ""
echo "Next steps:"
echo "  1. sudo -u $SERVICE_USER bash -c 'cd $REPO_DIR && uv sync'"
echo "  2. sudo /usr/local/bin/social-surveyor-load-env opendata"
echo "  3. sudo systemctl enable --now social-surveyor@opendata"
echo "  4. sudo journalctl -u social-surveyor@opendata -f"
