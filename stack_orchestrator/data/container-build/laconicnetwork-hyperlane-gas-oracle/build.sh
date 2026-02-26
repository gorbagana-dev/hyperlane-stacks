#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

docker build -t laconicnetwork/hyperlane-gas-oracle:local \
  ${build_command_args} \
  ~/cerc/hyperlane-stacks/hyperlane-gas-oracle
