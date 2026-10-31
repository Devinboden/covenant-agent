#!/bin/sh
# Seed the volume on first boot; the image's data is the starting point, the volume keeps decisions across deploys.
set -e
H="${COVENANT_HOME:-/data/home}"
mkdir -p "$H/data"
[ -d "$H/data/abstract" ] || cp -r /app/data/abstract "$H/data/abstract"
[ -f "$H/data/deals.json" ] || cp /app/data/deals.json "$H/data/deals.json"
python /app/deploy/sync_abstracts.py /app/data/abstract/v1 "$H/data/abstract/v1" || echo "sync: skipped (error)"
ln -sfn /app/data/pdf "$H/data/pdf"
ln -sfn /app/data/manifest.csv "$H/data/manifest.csv"
exec uvicorn covenant_engine.hosted:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*'
