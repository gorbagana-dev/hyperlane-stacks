#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# No upstream repo — metadata is committed alongside the Dockerfile; the build
# context is this directory.
docker build -t gorbagana-dev/hyperlane-hasura:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  ${SCRIPT_DIR}
