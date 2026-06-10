# SO Native WebSocket Support for k8s http-proxy: Design

**Date:** 2026-06-10
**Status:** Validated design
**Tracking:** `hyp-d34.7` (gates the prod/staging WS rollout for fast bridging;
also un-breaks public RPC WS for wallet users independently of the bridge)
**Repo touched:** `stack-orchestrator` (the gorbagana-rpc spec already declares
the right thing; it starts working unchanged)

Make `websocket: true` in a deployment spec's `network.http-proxy` routes real.
Today SO silently ignores the key; the live workaround
(`patch-caddy-websocket.sh`) patches Caddy's admin API in-memory and is broken
in production (`wss://rpc.gorbagana.wtf/` → 502 while HTTP RPC is healthy).

---

## 1. Background

### The constraint chain

1. Solana convention: the public WS URL is the **same host+path** as the HTTP
   RPC URL (web3.js derives `wss://host/` from `https://host/`; wallets rely
   on it). Every Solana RPC provider (Helius, Triton, …) fronts their nodes
   with a proxy that splits on the `Upgrade` header. We are in the standard
   Solana-provider situation, not an exotic one.
2. agave serves HTTP RPC and pubsub WS on **different ports** (8899 / 8900),
   so something must split same-URL traffic by header.
3. Caddy natively supports that split — it's a five-line config. But SO
   configures Caddy **through the k8s Ingress API**, which can only express
   `host + path → one backend`. No header matching exists in the Ingress
   schema (this poverty is why nginx grew snippets, Traefik grew CRDs, and
   the Gateway API exists).
4. The caddy-ingress-controller could bridge the gap with a custom
   annotation, but we have no write access to the fork
   (`LaconicNetwork/ingress`, image `ghcr.io/laconicnetwork/caddy-ingress`).
   Controller changes are off the table.

### Why the current state is broken

- `cluster_info.py:get_ingress()` (`deploy/k8s/cluster_info.py:209-291`)
  emits **both** `path: /` routes as duplicate Ingress paths with different
  backends — undefined which the controller honors. `websocket: true` is
  dropped on the floor.
- `patch-caddy-websocket.sh` PUTs a route via the Caddy admin API: in-memory
  (lost on Caddy restart **and on every controller config sync**), dials a
  `{deployment-id}-service` FQDN that goes stale on redeploy, and forces
  ingress protocols to `["h1"]` globally. The observed 502 matches a stale
  patched route.

## 2. Approach: SO-generated WebSocket mux

When a deployment's http-proxy contains `websocket: true` routes, SO
generates a small in-cluster Caddy ("ws-mux") that performs the header split,
and points the Ingress at it. The five-line Caddy config the situation calls
for is written at the one layer where we control real Caddy config instead of
going through the Ingress API's keyhole.

```
client ──TLS──▶ controller Caddy ──▶ ws-mux ──▶ agave-rpc:8899  (HTTP)
        (Ingress: host+path → mux)  (Upgrade?) ─▶ agave-rpc:8900  (WS)
```

Everything SO creates is declarative k8s state regenerated on deploy:
no admin-API surgery, nothing lost on restarts, no stale FQDNs.

**Rejected alternatives:**
- *Controller annotation* — right end-state, blocked on fork access. The
  design migrates cleanly if that changes (spec semantics identical; SO
  stops generating the mux). A gorbagana-dev controller fork via the
  existing `caddy-ingress-image` spec override remains possible later.
- *Self-healing admin-API patch* — institutionalizes the bypass; fights the
  controller's own config regeneration; keeps the global `h1` side effect.
- *Separate WS path/hostname* — plain Ingress could express it, but breaks
  web3.js URL derivation; public wallet users lose WS. Rejected per the
  same-URL requirement.

## 3. Spec semantics

```yaml
network:
  http-proxy:
    - host-name: rpc.gorbagana.wtf
      routes:
        - path: /
          proxy-to: agave-rpc:8899
        - path: /
          proxy-to: agave-rpc:8900
          websocket: true
```

- A `websocket: true` route **pairs** with the plain route sharing its
  (host, path): plain route serves HTTP, ws route serves upgrades.
- A `websocket: true` route with **no** plain twin is valid: the mux
  forwards everything at that (host, path) to the ws backend (no header
  match needed — degenerate case).
- Two **plain** routes sharing (host, path) become a hard validation error
  at `deploy create` (today's silent-conflict bug, now loud).
- The existing `gorbagana-rpc/deployment/spec.yml` is already in the
  correct form and starts working without edits.

## 4. Components (all in `stack-orchestrator`)

### 4.1 Spec parsing + validation (`deploy/spec.py`)

`get_http_proxy()` callers gain access to the `websocket` flag (parse,
default `false`). New validation at deploy create:

- duplicate plain (host, path) → error
- more than one `websocket: true` route per (host, path) → error
- `websocket: true` route whose `proxy-to` container has no exposed port →
  same handling as plain routes today

### 4.2 Mux generation (new `deploy/k8s/ws_mux.py`, used by `deploy_k8s.py`)

Generated only when the spec has ≥1 websocket route. One mux per deployment
serving all its websocket routes:

- **ConfigMap** `{app}-ws-mux-config`: a rendered Caddyfile. One site block
  per host (the controller preserves the `Host` header), one `handle` block
  per path within it; inside, the `@ws` matcher
  (`header Connection *Upgrade*`, `header Upgrade websocket`) routes to the
  ws backend service, fallthrough to the paired HTTP backend service.
  Backends are the same cluster Service FQDNs the Ingress would have used
  (resolved via the existing `_resolve_service_name_for_container`),
  rendered fresh at each deploy.
- **Deployment** `{app}-ws-mux`: one replica of the stock `caddy:2-alpine`
  image (new constant + spec override key `ws-mux-image`, mirroring the
  `caddy-ingress-image` pattern), mounting the Caddyfile read-only,
  listening on `:8080`, small requests/limits (e.g. 16Mi/50m), TCP
  readiness probe on 8080.
- **Service** `{app}-ws-mux` on 8080.

Lifecycle matches every other generated object: created at `deployment
start`, removed at `stop`/`down`, regenerated on redeploy.

### 4.3 Ingress generation (`deploy/k8s/cluster_info.py:get_ingress()`)

For each (host, path) that has a websocket route: emit **one** Ingress path
→ `{app}-ws-mux:8080` (replacing both the plain and ws entries). All other
paths unchanged. Result: every (host, path) maps to exactly one backend —
the duplicate-paths bug is structurally gone.

`_get_readiness_probe_ports()` / port collection (`cluster_info.py:296-330`)
keep deriving from `proxy-to` targets (TCP probes are valid against a WS
port); the mux's own port is handled by its Deployment, not by these maps.

### 4.4 TLS / certs

Unchanged. The controller still terminates TLS per host via cert-manager;
the mux speaks plain HTTP in-cluster.

## 5. Error handling

- Spec violations (§4.1) fail `deploy create` with messages naming the
  conflicting routes.
- ws backend down: mux returns 502 and its logs name the dialed service —
  strictly better than today's silent admin-API drift.
- Mux pod down: Ingress backend unavailable → controller 502s for that
  (host, path); readiness probe + 1-replica Deployment self-heals.

## 6. Risk: upgrade passthrough at the controller

The old patch forced ingress protocols to `["h1"]`. WS clients negotiate
HTTP/1.1 via ALPN and Caddy's `reverse_proxy` passes upgrades natively, so
the controller's standard generated route to the mux is expected to work
untouched. **This is the first thing the implementation verifies** (spike: a
kind cluster, dummy ws backend, 101-handshake through the controller →
mux). If the bundled controller Caddy version has an h2-upgrade quirk we
cannot config around via its global-options ConfigMap, the fallback is the
gorbagana-dev controller fork + `caddy-ingress-image` override — surfaced
then, not silently absorbed.

## 7. Testing

- **Unit (SO):** spec validation cases (§4.1); Caddyfile rendering (single
  route, paired routes, multi-host, degenerate ws-only); `get_ingress()`
  single-backend-per-(host,path) property with and without ws routes.
- **Integration (SO, kind):** deploy a fixture stack with paired routes;
  assert `curl --http1.1 -H "Upgrade: websocket" …` → 101 through the full
  controller→mux chain, plain HTTP still 200, and both survive a controller
  pod restart and a redeploy (the two failure modes of the old patch).
- **Live (gorbagana-rpc):** after SO upgrade + redeploy: WS handshake and a
  `slotSubscribe` round-trip against `wss://rpc.gorbagana.wtf/`; HTTP
  `getHealth` unchanged; delete `patch-caddy-websocket.sh` from the
  gorbagana-rpc repo.

## 8. Out of scope

- Gateway API migration (the long-term industry answer to Ingress's
  header-matching poverty — separate effort).
- Controller fork / annotation support (migration path documented in §2).
- The bridge-side WS work itself
  (`2026-06-10-websocket-fast-bridging-design.md`) — this design only
  unblocks its gorchain endpoint dependency.
