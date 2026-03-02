#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Copy supporting files into the build context so Dockerfile can COPY them
cp ${SCRIPT_DIR}/entrypoint.sh ${CERC_REPO_BASE_DIR}/hyperlane-monorepo/entrypoint.sh

docker build -t laconic/hyperlane-svm-deployer:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  ${CERC_REPO_BASE_DIR}/hyperlane-monorepo
