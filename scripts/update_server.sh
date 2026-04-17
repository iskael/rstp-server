#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/rstp-server}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-rstp-server}"
SERVICE_USER="${SERVICE_USER:-rstp}"
SERVICE_GROUP="${SERVICE_GROUP:-rstp}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root or with sudo."
  exit 1
fi

if [[ ! -d "${REPO_DIR}/.git" ]]; then
  echo "Expected a git checkout at ${REPO_DIR}."
  exit 1
fi

git -C "${REPO_DIR}" fetch origin
git -C "${REPO_DIR}" checkout "${BRANCH}"
git -C "${REPO_DIR}" pull --ff-only origin "${BRANCH}"

"${REPO_DIR}/.venv/bin/pip" install -r "${REPO_DIR}/requirements.txt"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${REPO_DIR}"

systemctl daemon-reload
systemctl restart "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}"
