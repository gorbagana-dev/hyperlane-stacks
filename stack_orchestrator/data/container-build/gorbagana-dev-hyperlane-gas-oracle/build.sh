#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

docker build -t gorbagana-dev/hyperlane-gas-oracle:local \
  ${build_command_args} \
  ${CERC_REPO_BASE_DIR}/hyperlane-stacks/hyperlane-gas-oracle
