#!/usr/bin/env bash
# Run on a machine WITH internet (the laptop that runs ./update-manny.sh).
# Builds the image and writes it to dist/cosmanagement-image.tar.gz, which
# ./update-manny.sh carries to the VM along with the rest of the tree
# (load it there with scripts/load-image.sh). The VM never needs a registry or PyPI.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker build -t cosmanagement:latest .
mkdir -p dist
docker save cosmanagement:latest | gzip > dist/cosmanagement-image.tar.gz
echo "OK: dist/cosmanagement-image.tar.gz - now run ./update-manny.sh"
