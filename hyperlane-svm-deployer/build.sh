#!/usr/bin/env bash
# build.sh — Build the hyperlane-svm-deployer Docker image
# Usage: ./build.sh [--tag TAG] [--no-cache]
set -euo pipefail

IMAGE_NAME="hyperlane-svm-deployer"
IMAGE_TAG="local"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            IMAGE_TAG="$2"
            shift 2
            ;;
        --no-cache)
            EXTRA_ARGS+=("--no-cache")
            shift
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Building ${IMAGE_NAME}:${IMAGE_TAG}"
echo "    Context: ${SCRIPT_DIR}"

docker build \
    -t "${IMAGE_NAME}:${IMAGE_TAG}" \
    "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" \
    "${SCRIPT_DIR}"

echo "==> Build complete: ${IMAGE_NAME}:${IMAGE_TAG}"
echo "    Image size: $(docker image inspect "${IMAGE_NAME}:${IMAGE_TAG}" --format='{{.Size}}' | numfmt --to=iec 2>/dev/null || docker image inspect "${IMAGE_NAME}:${IMAGE_TAG}" --format='{{.Size}}')"
