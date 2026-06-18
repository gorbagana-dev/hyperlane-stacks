#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Build context is the monorepo; the Dockerfile copies only monorepo sources
# (binaries + config). The entrypoint + seed SQL are mounted at runtime from the
# scraper-config configmap, so nothing is staged into the context here.
MONOREPO_DIR="${CERC_REPO_BASE_DIR}/hyperlane-monorepo"

DOCKER_BUILDKIT=1 docker build -t gorbagana-dev/hyperlane-scraper:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  "${MONOREPO_DIR}"
