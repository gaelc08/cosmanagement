#!/usr/bin/env bash
# Run on the VM, from the synced checkout: loads the image and (re)starts the container.
# Needs ./data/ with config.json, .env, storage_locations.txt and the CA bundle.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -f dist/cosmanagement-image.tar.gz ]] || { echo "dist/cosmanagement-image.tar.gz missing: run scripts/build-image.sh on the laptop first" >&2; exit 1; }
[[ -f data/config.json ]] || { echo "data/config.json missing (copy config.example.json there and edit it)" >&2; exit 1; }
gunzip -c dist/cosmanagement-image.tar.gz | docker load
docker compose up -d --no-build
echo "UI on the VM's 127.0.0.1:8767 - from your machine: ssh -L 8767:127.0.0.1:8767 <vm>"
