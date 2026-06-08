#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

REPO_DIR=${CERC_REPO_BASE_DIR}/hyperlane-warp-ui-template

cleanup() {
  # Remove files we added to the build context
  rm -f "${REPO_DIR}/entrypoint.sh"
}
trap cleanup EXIT

# Copy supporting files into the build context so Dockerfile can COPY them
cp ${SCRIPT_DIR}/entrypoint.sh ${REPO_DIR}/entrypoint.sh

docker build -t gorbagana-dev/hyperlane-warp-ui:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  ${REPO_DIR}
