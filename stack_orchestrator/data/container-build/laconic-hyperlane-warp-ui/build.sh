#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

REPO_DIR=${CERC_REPO_BASE_DIR}/hyperlane-warp-ui-template

# Save original files that we'll patch, so we can restore them after build
ORIG_WALLET_CTX="${REPO_DIR}/src/features/wallet/context/SolanaWalletContext.tsx"
ORIG_WHITELIST="${REPO_DIR}/src/consts/warpRouteWhitelist.ts"
BACKUP_WALLET_CTX=""
BACKUP_WHITELIST=""
if [ -f "${ORIG_WALLET_CTX}" ]; then
  BACKUP_WALLET_CTX=$(mktemp)
  cp "${ORIG_WALLET_CTX}" "${BACKUP_WALLET_CTX}"
fi
if [ -f "${ORIG_WHITELIST}" ]; then
  BACKUP_WHITELIST=$(mktemp)
  cp "${ORIG_WHITELIST}" "${BACKUP_WHITELIST}"
fi

cleanup() {
  # Remove files we added to the build context
  rm -f "${REPO_DIR}/entrypoint.sh"
  rm -f "${REPO_DIR}/fix-numeric-types.js"
  rm -rf "${REPO_DIR}/configs"

  # Restore original SolanaWalletContext.tsx
  if [ -n "${BACKUP_WALLET_CTX}" ] && [ -f "${BACKUP_WALLET_CTX}" ]; then
    cp "${BACKUP_WALLET_CTX}" "${ORIG_WALLET_CTX}"
    rm -f "${BACKUP_WALLET_CTX}"
  fi

  # Restore original warpRouteWhitelist.ts
  if [ -n "${BACKUP_WHITELIST}" ] && [ -f "${BACKUP_WHITELIST}" ]; then
    cp "${BACKUP_WHITELIST}" "${ORIG_WHITELIST}"
    rm -f "${BACKUP_WHITELIST}"
  fi
}
trap cleanup EXIT

# Copy supporting files into the build context so Dockerfile can COPY them
cp ${SCRIPT_DIR}/entrypoint.sh ${REPO_DIR}/entrypoint.sh
cp ${SCRIPT_DIR}/fix-numeric-types.js ${REPO_DIR}/fix-numeric-types.js
cp -r ${SCRIPT_DIR}/configs ${REPO_DIR}/configs

# Apply source patches
cp ${SCRIPT_DIR}/patches/SolanaWalletContext.tsx \
   ${REPO_DIR}/src/features/wallet/context/SolanaWalletContext.tsx
cp ${SCRIPT_DIR}/patches/warpRouteWhitelist.ts \
   ${REPO_DIR}/src/consts/warpRouteWhitelist.ts

docker build -t laconic/hyperlane-warp-ui:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  ${REPO_DIR}
