#!/bin/sh
set -eu

mkdir -p "${REPORTS_DIR:-/data/reports}"
if [ ! -f "${MAPPING_PATH:-/data/mapping.txt}" ]; then
    cp /app/mapping.txt "${MAPPING_PATH:-/data/mapping.txt}"
fi

exec python3 /app/bot.py
