#!/bin/bash
set -e

APP_DIR="/home/ubuntu/project"
DATA_DIR="${APP_DIR}/data"
PROJECT_ID="$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/project/project-id)"
BUCKET="gs://${PROJECT_ID}-mc-backups"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/tmp/mc-data-${TS}.tar.gz"

# 1) บังคับ flush ก่อน
docker exec mc-server rcon-cli save-all flush

# 2) บีบอัดทั้ง data (ครอบคลุม world/plugins/config ทั้งหมด)
tar -C "${APP_DIR}" -czf "${OUT}" data

# 3) อัปโหลดไป GCS
gsutil -q cp "${OUT}" "${BUCKET}/"

# 4) ลบไฟล์ชั่วคราว
rm -f "${OUT}"
