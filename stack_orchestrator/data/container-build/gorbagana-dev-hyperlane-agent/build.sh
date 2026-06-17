#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Build context is hyperlane-monorepo. The KMS/S3 customizations live in the
# gorbagana fork as commits — no build-time patches.
MONOREPO_DIR="${CERC_REPO_BASE_DIR}/hyperlane-monorepo"

DOCKER_BUILDKIT=1 docker build -t gorbagana-dev/hyperlane-agent:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  "${MONOREPO_DIR}"
