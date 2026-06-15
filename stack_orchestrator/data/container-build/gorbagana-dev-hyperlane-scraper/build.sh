#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Build context is hyperlane-monorepo; copy our entrypoint into it so the
# Dockerfile COPY works (mirrors the agent build copying its patch files in).
MONOREPO_DIR="${CERC_REPO_BASE_DIR}/hyperlane-monorepo"
DEST="${MONOREPO_DIR}/stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper"
mkdir -p "${DEST}"
cp "${SCRIPT_DIR}/entrypoint.sh" "${DEST}/"

cleanup() {
  rm -rf "${MONOREPO_DIR}/stack_orchestrator"
}
trap cleanup EXIT

DOCKER_BUILDKIT=1 docker build -t gorbagana-dev/hyperlane-scraper:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  "${MONOREPO_DIR}"
