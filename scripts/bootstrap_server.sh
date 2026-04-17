#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-git@github.com:iskael/rstp-server.git}"
REPO_DIR="${REPO_DIR:-/opt/rstp-server}"
SERVICE_NAME="${SERVICE_NAME:-rstp-server}"
SERVICE_USER="${SERVICE_USER:-rstp}"
SERVICE_GROUP="${SERVICE_GROUP:-rstp}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root or with sudo."
  exit 1
fi

apt-get update
apt-get install -y git ffmpeg python3 python3-venv python3-pip

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home "${REPO_DIR}" "${SERVICE_USER}"
fi

mkdir -p "${REPO_DIR}"

if [[ ! -d "${REPO_DIR}/.git" ]]; then
  rm -rf "${REPO_DIR}"
  git clone "${REPO_URL}" "${REPO_DIR}"
else
  git -C "${REPO_DIR}" fetch origin
  git -C "${REPO_DIR}" checkout -B main origin/main
fi

mkdir -p "${REPO_DIR}/data/captures"
python3 -m venv "${REPO_DIR}/.venv"
"${REPO_DIR}/.venv/bin/pip" install --upgrade pip
"${REPO_DIR}/.venv/bin/pip" install -r "${REPO_DIR}/requirements.txt"

install -m 0644 "${REPO_DIR}/systemd/rstp-server.service" "/etc/systemd/system/${SERVICE_NAME}.service"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${REPO_DIR}"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}"
