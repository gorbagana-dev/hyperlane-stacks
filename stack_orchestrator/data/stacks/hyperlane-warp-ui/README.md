# hyperlane-warp-ui

Browser-based bridge UI for cross-chain token transfers using Hyperlane warp routes. Serves a Next.js app with runtime-injected chain and token configuration.

Built from the fork [gorbagana-dev/hyperlane-warp-ui-template](https://github.com/gorbagana-dev/hyperlane-warp-ui-template) at tag `v2.0.0-gorbagana.6`, which adds runtime route/chain config support and a slimmed client bundle (unused-protocol SDKs and EVM deploy artifacts dropped).

## Prerequisites

- A running `k8s-kind` cluster
- `laconic-so` (stack-orchestrator) installed
- `hyperlane-svm-deployer` stack deployed (needs mailbox addresses)
- `hyperlane-svm-warp-deployer` stack deployed (needs warp route addresses)

## 1. Build container

```bash
laconic-so --stack hyperlane-warp-ui setup-repositories
laconic-so --stack hyperlane-warp-ui build-containers
```

Builds `gorbagana-dev/hyperlane-warp-ui:local`.

## 2. Create deployment

```bash
laconic-so --stack hyperlane-warp-ui deploy init --output warp-ui-spec.yml
```

Edit `warp-ui-spec.yml` (see `deployment/spec-warp-ui.yml` for reference):

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-warp-ui
deploy-to: k8s-kind
config:
  GORCHAIN_RPC_URL: "https://gorchain-rpc.example.com"
  SOLANA_RPC_URL: "https://solana-rpc.example.com"
  GORCHAIN_DOMAIN_ID: "99999"
  SOLANA_DOMAIN_ID: "99998"
  GORCHAIN_CHAIN_ID: "99999"
  SOLANA_CHAIN_ID: "99998"
  GORCHAIN_MAILBOX: "<gorchain-mailbox-program-id>"
  SOLANA_MAILBOX: "<solana-mailbox-program-id>"
  NEXT_PUBLIC_WALLET_CONNECT_ID: "<walletconnect-project-id>"
configmaps:
  warp-ui-config: ./configmaps/warp-ui-config
network:
  http-proxy:
    - host-name: bridge.example.com
      routes:
        - path: /
          proxy-to: warp-ui:3000
```

```bash
laconic-so --stack hyperlane-warp-ui deploy create --spec-file warp-ui-spec.yml --deployment-dir warp-ui-deployment
```

The `http-proxy` section configures ingress routing. Update `host-name` to your domain.

The `warp-ui-config` ConfigMap carries `warpRoutes.yaml` (the runtime route/token config built by the `hyperlane-svm-warp-deployer`); the container renders `chains.yaml` from the chain env vars at startup. Routes are not baked into the image.

## 3. Start

```bash
laconic-so deployment --dir warp-ui-deployment start
```

## 4. Verify

```bash
# Check pod is running
kubectl get pods -l app=hyperlane-warp-ui

# Check UI is serving
kubectl port-forward svc/warp-ui 3000:3000
# Open http://localhost:3000
```
