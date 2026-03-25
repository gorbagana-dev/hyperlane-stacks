#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Build context is hyperlane-monorepo, but we need the patch file from
# hyperlane-stacks. Copy it into the monorepo temporarily so COPY works
# in the Dockerfile.
MONOREPO_DIR="${CERC_REPO_BASE_DIR}/hyperlane-monorepo"
PATCH_DEST="${MONOREPO_DIR}/stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent"
mkdir -p "${PATCH_DEST}"
cp "${SCRIPT_DIR}/kms-endpoint.patch" "${PATCH_DEST}/"
cp "${SCRIPT_DIR}/s3-path-style.patch" "${PATCH_DEST}/"

DOCKER_BUILDKIT=1 docker build -t gorbagana-dev/hyperlane-agent:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  "${MONOREPO_DIR}"

# Clean up temporary copy
rm -rf "${MONOREPO_DIR}/stack_orchestrator"
