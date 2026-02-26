#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Copy supporting files into the build context so Dockerfile can COPY them
cp ${SCRIPT_DIR}/entrypoint.sh ~/cerc/hyperlane-monorepo/entrypoint.sh

docker build -t laconicnetwork/hyperlane-svm-deployer:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  ~/cerc/hyperlane-monorepo
