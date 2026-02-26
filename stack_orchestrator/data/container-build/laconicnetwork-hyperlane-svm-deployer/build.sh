#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

docker build -t laconicnetwork/hyperlane-svm-deployer:local \
  -f ${SCRIPT_DIR}/../../../../hyperlane-svm-deployer/Dockerfile \
  ${build_command_args} \
  ${SCRIPT_DIR}/../../../../hyperlane-svm-deployer
