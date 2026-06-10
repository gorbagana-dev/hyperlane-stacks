# SO Native WebSocket Support (ws-mux) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `websocket: true` in SO deployment-spec http-proxy routes real, by generating a per-deployment Caddy mux that splits `Upgrade` traffic, so `wss://rpc.gorbagana.wtf/` works at the same URL as HTTP RPC.

**Architecture:** Pure route logic + Caddyfile rendering in a new `ws_mux.py` (no k8s imports, fully unit-testable); k8s object builders on `ClusterInfo`; creation wired into `K8sDeployer` next to the Ingress; validation hooked into `create_operation`. Down-cleanup is automatic via the existing `app.kubernetes.io/stack` label sweep.

**Tech Stack:** Python (stack-orchestrator), kubernetes Python client, Caddy 2 (stock `caddy:2-alpine` image), unittest + MagicMock (existing `tests/unit/` convention).

**Spec:** `docs/superpowers/specs/2026-06-10-so-websocket-ingress-design.md` (hyperlane-stacks repo). Tracking: `hyp-d34.7`.

**Repo:** ALL implementation in `/home/dev/git_puller/repos/stack-orchestrator`. Branch `websocket-mux` off `main`. The plan document lives in hyperlane-stacks.

**Risk gate (spec §6):** Tasks 1–6 are unit-tested code whose shape is correct regardless of the controller-passthrough question. Task 7 is the spike that answers it: if the 101 handshake fails at the controller→mux hop, STOP and report (fallback = gorbagana-dev controller fork via `caddy-ingress-image` override; do not improvise protocol forcing).

**Conventions:** run unit tests with `python3 -m pytest tests/unit/test_ws_mux.py -v` from the repo root. Commit after each task; never push (user pushes). Commit trailer:
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

---

## File structure

| File | Responsibility |
|---|---|
| `stack_orchestrator/constants.py` (modify) | `ws_mux_image_key`, `default_ws_mux_image` |
| `stack_orchestrator/deploy/spec.py` (modify) | `get_ws_mux_image()` accessor |
| `stack_orchestrator/deploy/k8s/ws_mux.py` (create) | Pure: route validation, mux-entry collection, Caddyfile rendering. NO kubernetes imports. |
| `stack_orchestrator/deploy/k8s/cluster_info.py` (modify) | `get_ingress()` single-backend fix; `get_ws_mux_resources()` V1 object builders |
| `stack_orchestrator/deploy/k8s/deploy_k8s.py` (modify) | `_create_ws_mux()` wired into `up` before `_create_ingress()` |
| `stack_orchestrator/deploy/deployment_create.py` (modify) | validation call in `create_operation` |
| `tests/unit/test_ws_mux.py` (create) | all unit tests |
| `stack_orchestrator/data/stacks/test-websocket/stack.yml`, `stack_orchestrator/data/compose/docker-compose-test-websocket.yml` (create) | integration fixture stack (Task 7) |

---

### Task 1: Constants and spec accessor

**Files:**
- Modify: `stack_orchestrator/constants.py` (after line 52, next to `default_caddy_ingress_image`)
- Modify: `stack_orchestrator/deploy/spec.py` (next to `get_caddy_ingress_image`, ~line 307)
- Test: `tests/unit/test_ws_mux.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ws_mux.py`:

```python
# tests/unit/test_ws_mux.py
"""Unit tests for websocket mux support (spec key, route logic, rendering)."""
import unittest

from stack_orchestrator import constants


class TestWsMuxImageKey(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(constants.ws_mux_image_key, "ws-mux-image")
        self.assertEqual(constants.default_ws_mux_image, "caddy:2-alpine")

    def test_spec_accessor_default_and_override(self):
        from stack_orchestrator.deploy.spec import Spec

        spec = Spec(obj={})
        self.assertIsNone(spec.get_ws_mux_image())
        spec = Spec(obj={"ws-mux-image": "caddy:2.8-alpine"})
        self.assertEqual(spec.get_ws_mux_image(), "caddy:2.8-alpine")
```

Note: check `Spec.__init__` signature first (`stack_orchestrator/deploy/spec.py`); existing unit tests construct it — follow whatever `tests/unit/test_recreate_job.py` does if `Spec(obj={})` isn't the right form.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_ws_mux.py -v`
Expected: FAIL — `AttributeError: module 'stack_orchestrator.constants' has no attribute 'ws_mux_image_key'`

- [ ] **Step 3: Implement**

In `constants.py`, after `default_caddy_ingress_image`:

```python
ws_mux_image_key = "ws-mux-image"
default_ws_mux_image = "caddy:2-alpine"
```

In `spec.py`, next to `get_caddy_ingress_image()` (mirror its body/style):

```python
def get_ws_mux_image(self) -> typing.Optional[str]:
    """Optional override for the websocket mux Caddy image."""
    return self.obj.get(constants.ws_mux_image_key)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_ws_mux.py -v` — PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/constants.py stack_orchestrator/deploy/spec.py tests/unit/test_ws_mux.py
git commit -m "feat: ws-mux-image spec key for websocket mux support"
```

---

### Task 2: Route validation (pure)

**Files:**
- Create: `stack_orchestrator/deploy/k8s/ws_mux.py`
- Test: `tests/unit/test_ws_mux.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_ws_mux.py`)

```python
from stack_orchestrator.deploy.k8s.ws_mux import validate_http_proxy_routes


def _proxy(host, routes):
    return {"host-name": host, "routes": routes}


class TestValidateHttpProxyRoutes(unittest.TestCase):
    def test_empty_ok(self):
        validate_http_proxy_routes([])

    def test_paired_routes_ok(self):
        validate_http_proxy_routes([_proxy("a.example.com", [
            {"path": "/", "proxy-to": "app:8899"},
            {"path": "/", "proxy-to": "app:8900", "websocket": True},
        ])])

    def test_ws_only_route_ok(self):
        validate_http_proxy_routes([_proxy("a.example.com", [
            {"path": "/", "proxy-to": "app:8900", "websocket": True},
        ])])

    def test_duplicate_plain_routes_error(self):
        with self.assertRaises(ValueError) as ctx:
            validate_http_proxy_routes([_proxy("a.example.com", [
                {"path": "/", "proxy-to": "app:8899"},
                {"path": "/", "proxy-to": "other:9000"},
            ])])
        self.assertIn("a.example.com", str(ctx.exception))
        self.assertIn("/", str(ctx.exception))

    def test_duplicate_ws_routes_error(self):
        with self.assertRaises(ValueError):
            validate_http_proxy_routes([_proxy("a.example.com", [
                {"path": "/", "proxy-to": "app:8900", "websocket": True},
                {"path": "/", "proxy-to": "app:8901", "websocket": True},
            ])])

    def test_same_path_different_hosts_ok(self):
        validate_http_proxy_routes([
            _proxy("a.example.com", [{"path": "/", "proxy-to": "app:8899"}]),
            _proxy("b.example.com", [{"path": "/", "proxy-to": "app:8899"}]),
        ])
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`/`ImportError` for `ws_mux`.

- [ ] **Step 3: Implement** — create `stack_orchestrator/deploy/k8s/ws_mux.py`:

```python
# Copyright header: copy the SPDX/license header from cluster_info.py
"""Websocket mux support for k8s http-proxy.

The k8s Ingress API cannot express header-based routing (same host+path to
one backend for HTTP, another for websocket upgrades). When a spec declares
`websocket: true` routes, SO generates a small Caddy "ws-mux" that performs
the Upgrade split, and points the Ingress at it. This module holds the pure
logic: route validation, mux entry collection, Caddyfile rendering. No
kubernetes imports here — object construction lives in cluster_info.py.
"""

MUX_PORT = 8080


def validate_http_proxy_routes(http_proxy_info_list):
    """Reject route sets the ingress+mux model cannot serve unambiguously.

    Raises ValueError naming the conflicting host/path.
    """
    for proxy in http_proxy_info_list or []:
        host = proxy["host-name"]
        plain_seen = set()
        ws_seen = set()
        for route in proxy.get("routes", []):
            path = route["path"]
            if route.get("websocket"):
                if path in ws_seen:
                    raise ValueError(
                        f"http-proxy: multiple websocket routes for "
                        f"{host}{path}"
                    )
                ws_seen.add(path)
            else:
                if path in plain_seen:
                    raise ValueError(
                        f"http-proxy: duplicate routes for {host}{path} — "
                        f"each host+path may have at most one plain route "
                        f"and one websocket route"
                    )
                plain_seen.add(path)
```

- [ ] **Step 4: Run** — `python3 -m pytest tests/unit/test_ws_mux.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/deploy/k8s/ws_mux.py tests/unit/test_ws_mux.py
git commit -m "feat: validate http-proxy routes for websocket mux model"
```

---

### Task 3: Mux entry collection + Caddyfile rendering (pure)

**Files:**
- Modify: `stack_orchestrator/deploy/k8s/ws_mux.py`
- Test: `tests/unit/test_ws_mux.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
from stack_orchestrator.deploy.k8s.ws_mux import (
    collect_mux_entries,
    render_mux_caddyfile,
)


def _resolve(container_name):
    return f"{container_name}-svc"


class TestCollectMuxEntries(unittest.TestCase):
    def test_no_ws_routes_returns_empty(self):
        entries = collect_mux_entries(
            [_proxy("a.example.com", [{"path": "/", "proxy-to": "app:8899"}])],
            _resolve,
        )
        self.assertEqual(entries, [])

    def test_paired(self):
        entries = collect_mux_entries([_proxy("a.example.com", [
            {"path": "/", "proxy-to": "app:8899"},
            {"path": "/", "proxy-to": "app:8900", "websocket": True},
        ])], _resolve)
        self.assertEqual(entries, [{
            "host": "a.example.com",
            "path": "/",
            "ws_backend": "app-svc:8900",
            "http_backend": "app-svc:8899",
        }])

    def test_ws_only(self):
        entries = collect_mux_entries([_proxy("a.example.com", [
            {"path": "/", "proxy-to": "app:8900", "websocket": True},
        ])], _resolve)
        self.assertEqual(entries[0]["http_backend"], None)


class TestRenderMuxCaddyfile(unittest.TestCase):
    def test_paired_root(self):
        out = render_mux_caddyfile([{
            "host": "a.example.com", "path": "/",
            "ws_backend": "app-svc:8900", "http_backend": "app-svc:8899",
        }])
        self.assertIn("admin off", out)
        self.assertIn("auto_https off", out)
        self.assertIn(":8080 {", out)
        self.assertIn("host a.example.com", out)
        self.assertIn("header Upgrade websocket", out)
        self.assertIn("reverse_proxy app-svc:8900", out)
        self.assertIn("reverse_proxy app-svc:8899", out)
        # ws backend must appear before the http fallback
        self.assertLess(out.index("app-svc:8900"), out.index("app-svc:8899"))

    def test_ws_only_has_no_header_matcher(self):
        out = render_mux_caddyfile([{
            "host": "a.example.com", "path": "/",
            "ws_backend": "app-svc:8900", "http_backend": None,
        }])
        self.assertNotIn("header Upgrade", out)
        self.assertIn("reverse_proxy app-svc:8900", out)

    def test_multi_host_and_subpath_ordering(self):
        out = render_mux_caddyfile([
            {"host": "a.example.com", "path": "/",
             "ws_backend": "a-svc:8900", "http_backend": "a-svc:8899"},
            {"host": "a.example.com", "path": "/sub",
             "ws_backend": "a-svc:9900", "http_backend": "a-svc:9899"},
            {"host": "b.example.com", "path": "/",
             "ws_backend": "b-svc:8900", "http_backend": "b-svc:8899"},
        ])
        self.assertIn("host b.example.com", out)
        # longest path first within a host so /sub isn't shadowed by /
        self.assertLess(out.index("/sub*"), out.index("a-svc:8900"))
```

- [ ] **Step 2: Run to verify failure** — ImportError for `collect_mux_entries`.

- [ ] **Step 3: Implement** (append to `ws_mux.py`)

```python
def collect_mux_entries(http_proxy_info_list, resolve_service):
    """Collect websocket mux entries: one per (host, path) with a ws route.

    resolve_service: callable(container_name) -> k8s service name.
    Returns a list of dicts:
      {host, path, ws_backend: "svc:port", http_backend: "svc:port" | None}
    """
    entries = []
    for proxy in http_proxy_info_list or []:
        host = proxy["host-name"]
        plain = {}
        ws = {}
        for route in proxy.get("routes", []):
            container, port = route["proxy-to"].split(":")
            backend = f"{resolve_service(container)}:{int(port)}"
            if route.get("websocket"):
                ws[route["path"]] = backend
            else:
                plain[route["path"]] = backend
        for path, ws_backend in ws.items():
            entries.append({
                "host": host,
                "path": path,
                "ws_backend": ws_backend,
                "http_backend": plain.get(path),
            })
    return entries


def _path_matcher(path):
    # Prefix semantics to match Ingress path_type=Prefix
    return None if path == "/" else f"{path.rstrip('/')}*"


def render_mux_caddyfile(entries):
    """Render the ws-mux Caddyfile for the given entries.

    One handle block per host; within it, longest path first so subpaths
    are not shadowed; per path, an Upgrade-header matcher routes to the ws
    backend with fallthrough to the paired HTTP backend (or a direct proxy
    when there is no pair).
    """
    lines = [
        "{",
        "\tadmin off",
        "\tauto_https off",
        "}",
        "",
        f":{MUX_PORT} {{",
    ]
    hosts = sorted({e["host"] for e in entries})
    for h_idx, host in enumerate(hosts):
        host_entries = sorted(
            (e for e in entries if e["host"] == host),
            key=lambda e: len(e["path"]),
            reverse=True,
        )
        lines.append(f"\t@host{h_idx} host {host}")
        lines.append(f"\thandle @host{h_idx} {{")
        for p_idx, entry in enumerate(host_entries):
            matcher = _path_matcher(entry["path"])
            indent = "\t\t"
            if matcher:
                lines.append(f"{indent}handle {matcher} {{")
                indent += "\t"
            if entry["http_backend"]:
                ws_name = f"@ws{h_idx}_{p_idx}"
                lines.append(f"{indent}{ws_name} {{")
                lines.append(f"{indent}\theader Connection *Upgrade*")
                lines.append(f"{indent}\theader Upgrade websocket")
                lines.append(f"{indent}}}")
                lines.append(f"{indent}handle {ws_name} {{")
                lines.append(
                    f"{indent}\treverse_proxy {entry['ws_backend']}"
                )
                lines.append(f"{indent}}}")
                lines.append(f"{indent}handle {{")
                lines.append(
                    f"{indent}\treverse_proxy {entry['http_backend']}"
                )
                lines.append(f"{indent}}}")
            else:
                lines.append(
                    f"{indent}reverse_proxy {entry['ws_backend']}"
                )
            if matcher:
                lines.append("\t\t}")
        lines.append("\t}")
    lines.append("}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run** — all tests PASS. Also sanity-check the rendered config with real Caddy:

```bash
python3 - <<'EOF' > /tmp/Caddyfile
from stack_orchestrator.deploy.k8s.ws_mux import render_mux_caddyfile
print(render_mux_caddyfile([{"host": "a.example.com", "path": "/",
    "ws_backend": "app-svc:8900", "http_backend": "app-svc:8899"}]), end="")
EOF
docker run --rm -v /tmp/Caddyfile:/etc/caddy/Caddyfile caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
```
Expected: `Valid configuration`. If Caddy rejects the nesting, fix the renderer until `caddy validate` passes — this gate is part of the task.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/deploy/k8s/ws_mux.py tests/unit/test_ws_mux.py
git commit -m "feat: collect websocket mux entries and render mux Caddyfile"
```

---

### Task 4: ClusterInfo — single-backend ingress + mux k8s objects

**Files:**
- Modify: `stack_orchestrator/deploy/k8s/cluster_info.py` (`get_ingress()` at ~209-291; new method after it)
- Test: `tests/unit/test_ws_mux.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
from unittest.mock import MagicMock


def _make_cluster_info(http_proxy):
    from stack_orchestrator.deploy.k8s.cluster_info import ClusterInfo

    ci = ClusterInfo.__new__(ClusterInfo)
    ci.app_name = "testapp"
    ci.stack_name = "test-stack"
    ci.parsed_pod_yaml_map = {"pod1": {"services": {"app": {}}}}
    ci.parsed_job_yaml_map = {}
    ci.spec = MagicMock()
    ci.spec.get_http_proxy.return_value = http_proxy
    ci.spec.get_ws_mux_image.return_value = None
    return ci


PAIRED = [_proxy("a.example.com", [
    {"path": "/", "proxy-to": "app:8899"},
    {"path": "/", "proxy-to": "app:8900", "websocket": True},
])]


class TestGetIngressWithWs(unittest.TestCase):
    def test_paired_routes_emit_single_mux_backend(self):
        ci = _make_cluster_info(PAIRED)
        ingress = ci.get_ingress(use_tls=False)
        paths = ingress.spec.rules[0].http.paths
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].backend.service.name, "testapp-ws-mux")
        self.assertEqual(paths[0].backend.service.port.number, 8080)

    def test_no_ws_routes_unchanged(self):
        ci = _make_cluster_info(
            [_proxy("a.example.com", [{"path": "/", "proxy-to": "app:8899"}])]
        )
        ingress = ci.get_ingress(use_tls=False)
        paths = ingress.spec.rules[0].http.paths
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].backend.service.name, "testapp-service")


class TestGetWsMuxResources(unittest.TestCase):
    def test_none_when_no_ws_routes(self):
        ci = _make_cluster_info(
            [_proxy("a.example.com", [{"path": "/", "proxy-to": "app:8899"}])]
        )
        self.assertIsNone(ci.get_ws_mux_resources())

    def test_objects_generated(self):
        ci = _make_cluster_info(PAIRED)
        res = ci.get_ws_mux_resources()
        cm, dep, svc = res["configmap"], res["deployment"], res["service"]
        self.assertEqual(cm.metadata.name, "testapp-ws-mux-config")
        self.assertIn("reverse_proxy testapp-service:8900", cm.data["Caddyfile"])
        self.assertEqual(dep.metadata.name, "testapp-ws-mux")
        # cleanup sweep finds it via the stack label
        self.assertEqual(
            dep.metadata.labels["app.kubernetes.io/stack"], "test-stack"
        )
        # pod template label is DISTINCT from the main app label so the
        # main app services never select mux pods
        self.assertEqual(
            dep.spec.template.metadata.labels["app"], "testapp-ws-mux"
        )
        self.assertEqual(
            svc.spec.selector["app"], "testapp-ws-mux"
        )
        self.assertEqual(svc.spec.ports[0].port, 8080)
        self.assertEqual(
            dep.spec.template.spec.containers[0].image, "caddy:2-alpine"
        )

    def test_image_override(self):
        ci = _make_cluster_info(PAIRED)
        ci.spec.get_ws_mux_image.return_value = "caddy:2.8-alpine"
        res = ci.get_ws_mux_resources()
        self.assertEqual(
            res["deployment"].spec.template.spec.containers[0].image,
            "caddy:2.8-alpine",
        )
```

- [ ] **Step 2: Run to verify failure** — first test fails: 2 paths emitted / no `get_ws_mux_resources`.

- [ ] **Step 3: Implement.**

Imports at top of `cluster_info.py`:

```python
from stack_orchestrator.deploy.k8s import ws_mux
```

In `get_ingress()`, replace the route loop body (`cluster_info.py:242-267`) with:

```python
                mux_entries = ws_mux.collect_mux_entries(
                    [http_proxy_info], self._resolve_service_name_for_container
                )
                mux_paths = {e["path"] for e in mux_entries}
                paths = []
                emitted_mux_paths = set()
                for route in http_proxy_info["routes"]:
                    path = route["path"]
                    if path in mux_paths:
                        # (host, path) has a websocket route: route the whole
                        # path to this deployment's ws-mux (single backend —
                        # the Ingress API cannot express the header split).
                        if path in emitted_mux_paths:
                            continue
                        emitted_mux_paths.add(path)
                        service_name = f"{self.app_name}-ws-mux"
                        proxy_to_port = ws_mux.MUX_PORT
                    else:
                        proxy_to = route["proxy-to"]
                        if opts.o.debug:
                            print(f"proxy config: {path} -> {proxy_to}")
                        container_name = proxy_to.split(":")[0]
                        proxy_to_port = int(proxy_to.split(":")[1])
                        service_name = self._resolve_service_name_for_container(
                            container_name
                        )
                    paths.append(
                        client.V1HTTPIngressPath(
                            path_type="Prefix",
                            path=path,
                            backend=client.V1IngressBackend(
                                service=client.V1IngressServiceBackend(
                                    name=service_name,
                                    port=client.V1ServiceBackendPort(
                                        number=proxy_to_port
                                    ),
                                )
                            ),
                        )
                    )
```

New method after `get_ingress()`:

```python
    def get_ws_mux_resources(self):
        """Build the websocket mux objects (ConfigMap, Deployment, Service)
        for this deployment, or None when the spec has no websocket routes.

        The mux performs the Upgrade-header split the Ingress API cannot
        express. Resource metadata carries the standard stack labels so the
        down sweep finds them; the pod template uses a distinct app label so
        the main app Services never select mux pods.
        """
        entries = ws_mux.collect_mux_entries(
            self.spec.get_http_proxy(),
            self._resolve_service_name_for_container,
        )
        if not entries:
            return None

        mux_name = f"{self.app_name}-ws-mux"
        caddyfile = ws_mux.render_mux_caddyfile(entries)
        image = self.spec.get_ws_mux_image() or constants.default_ws_mux_image
        pod_labels = self._stack_labels(extra={"app": mux_name})

        configmap = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=f"{mux_name}-config", labels=self._stack_labels()
            ),
            data={"Caddyfile": caddyfile},
        )
        container = client.V1Container(
            name="ws-mux",
            image=image,
            ports=[client.V1ContainerPort(container_port=ws_mux.MUX_PORT)],
            volume_mounts=[
                client.V1VolumeMount(
                    name="caddyfile",
                    mount_path="/etc/caddy",
                    read_only=True,
                )
            ],
            readiness_probe=client.V1Probe(
                tcp_socket=client.V1TCPSocketAction(port=ws_mux.MUX_PORT),
                initial_delay_seconds=1,
                period_seconds=5,
            ),
            resources=client.V1ResourceRequirements(
                requests={"memory": "16Mi", "cpu": "10m"},
                limits={"memory": "64Mi", "cpu": "100m"},
            ),
        )
        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=mux_name, labels=self._stack_labels()
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(
                    match_labels={"app": mux_name}
                ),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=pod_labels),
                    spec=client.V1PodSpec(
                        containers=[container],
                        volumes=[
                            client.V1Volume(
                                name="caddyfile",
                                config_map=client.V1ConfigMapVolumeSource(
                                    name=f"{mux_name}-config"
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )
        service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=mux_name, labels=self._stack_labels()
            ),
            spec=client.V1ServiceSpec(
                ports=[
                    client.V1ServicePort(
                        port=ws_mux.MUX_PORT, target_port=ws_mux.MUX_PORT
                    )
                ],
                selector={"app": mux_name},
            ),
        )
        return {
            "configmap": configmap,
            "deployment": deployment,
            "service": service,
        }
```

Check that `constants` is already imported in `cluster_info.py` (it is — used for other keys); if the import is module-level `from stack_orchestrator import constants`, the code above works as-is.

- [ ] **Step 4: Run** — `python3 -m pytest tests/unit/test_ws_mux.py -v` — all PASS. Also run the whole unit suite to catch regressions: `python3 -m pytest tests/unit/ -v`.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/deploy/k8s/cluster_info.py tests/unit/test_ws_mux.py
git commit -m "feat: route websocket paths to a generated ws-mux; single backend per ingress path"
```

---

### Task 5: Deployer wiring — create mux objects at up

**Files:**
- Modify: `stack_orchestrator/deploy/k8s/deploy_k8s.py` (new `_create_ws_mux()`; call it immediately before the existing `self._create_ingress()` call site in the up flow — find with `grep -n "_create_ingress()" stack_orchestrator/deploy/k8s/deploy_k8s.py`)
- Test: `tests/unit/test_ws_mux.py`

- [ ] **Step 1: Write the failing tests** (append; follow the `TestCreateUserSecrets` mock pattern from `tests/unit/test_user_secrets.py`)

```python
from kubernetes.client.exceptions import ApiException


class TestCreateWsMux(unittest.TestCase):
    def setUp(self):
        from stack_orchestrator.deploy.k8s.deploy_k8s import K8sDeployer

        self.deployer = K8sDeployer.__new__(K8sDeployer)
        self.deployer.k8s_namespace = "test-ns"
        self.deployer.core_api = MagicMock()
        self.deployer.apps_api = MagicMock()
        self.deployer.cluster_info = MagicMock()

    def test_noop_when_no_mux(self):
        self.deployer.cluster_info.get_ws_mux_resources.return_value = None
        self.deployer._create_ws_mux()
        self.deployer.core_api.create_namespaced_config_map.assert_not_called()

    def test_creates_all_objects(self):
        cm, dep, svc = MagicMock(), MagicMock(), MagicMock()
        self.deployer.cluster_info.get_ws_mux_resources.return_value = {
            "configmap": cm, "deployment": dep, "service": svc,
        }
        self.deployer._create_ws_mux()
        self.deployer.core_api.create_namespaced_config_map.assert_called_once_with(
            namespace="test-ns", body=cm
        )
        self.deployer.apps_api.create_namespaced_deployment.assert_called_once_with(
            namespace="test-ns", body=dep
        )
        self.deployer.core_api.create_namespaced_service.assert_called_once_with(
            namespace="test-ns", body=svc
        )

    def test_409_replaces(self):
        cm, dep, svc = MagicMock(), MagicMock(), MagicMock()
        self.deployer.cluster_info.get_ws_mux_resources.return_value = {
            "configmap": cm, "deployment": dep, "service": svc,
        }
        conflict = ApiException(status=409)
        self.deployer.core_api.create_namespaced_config_map.side_effect = conflict
        self.deployer.apps_api.create_namespaced_deployment.side_effect = conflict
        self.deployer.core_api.create_namespaced_service.side_effect = conflict
        self.deployer._create_ws_mux()
        self.deployer.core_api.replace_namespaced_config_map.assert_called_once()
        self.deployer.apps_api.replace_namespaced_deployment.assert_called_once()
        self.deployer.core_api.replace_namespaced_service.assert_called_once()
```

Note: replicate how `opts` is handled in existing deployer unit tests — `test_user_secrets.py` shows whether `opts.o.dry_run`/`debug` need patching; mirror it (e.g. `@patch("stack_orchestrator.deploy.k8s.deploy_k8s.opts")`).

- [ ] **Step 2: Run to verify failure** — `AttributeError: ... no attribute '_create_ws_mux'`.

- [ ] **Step 3: Implement** — add to `K8sDeployer` next to `_create_ingress()`:

```python
    def _create_ws_mux(self):
        """Create or update the websocket mux objects (spec websocket: true).

        Must run before _create_ingress so the mux Service exists when the
        Ingress references it.
        """
        resources = self.cluster_info.get_ws_mux_resources()
        if not resources:
            return
        if opts.o.dry_run:
            print("Dry run: would create ws-mux resources")
            return
        ns = self.k8s_namespace

        def _create_or_replace(create_fn, replace_fn, body):
            try:
                create_fn(namespace=ns, body=body)
            except ApiException as e:
                if e.status == 409:
                    replace_fn(
                        name=body.metadata.name, namespace=ns, body=body
                    )
                else:
                    raise

        _create_or_replace(
            self.core_api.create_namespaced_config_map,
            self.core_api.replace_namespaced_config_map,
            resources["configmap"],
        )
        _create_or_replace(
            self.apps_api.create_namespaced_deployment,
            self.apps_api.replace_namespaced_deployment,
            resources["deployment"],
        )
        _create_or_replace(
            self.core_api.create_namespaced_service,
            self.core_api.replace_namespaced_service,
            resources["service"],
        )
        print("Created ws-mux resources")
```

Caveat the implementer must handle: with MagicMock bodies, `body.metadata.name` works; with real V1Service replace, k8s requires `resource_version`/`cluster_ip` carry-over (see `_create_nodeports`, `deploy_k8s.py:1100-1112`). Follow that precedent inside `_create_or_replace` for the Service: on 409, read the existing service and copy `metadata.resource_version` + `spec.cluster_ip` before replacing. Write the test for the service-409 path accordingly (assert `read_namespaced_service` called).

Then add the call in the up flow, immediately before `self._create_ingress()`:

```python
        self._create_ws_mux()
        self._create_ingress(...)   # existing call, unchanged
```

Down-cleanup needs **no change**: mux resources carry `app.kubernetes.io/stack` metadata labels and `_delete_resources_by_label` (`deploy_k8s.py:314-386`) sweeps Deployments, Services, and ConfigMaps by that selector.

- [ ] **Step 4: Run** — `python3 -m pytest tests/unit/ -v` — all PASS.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/deploy/k8s/deploy_k8s.py tests/unit/test_ws_mux.py
git commit -m "feat: create ws-mux resources at deployment up, before the ingress"
```

---

### Task 6: Validation at deploy create

**Files:**
- Modify: `stack_orchestrator/deploy/deployment_create.py` (`create_operation`, after `_check_volume_definitions(parsed_spec)` at line 979)
- Test: `tests/unit/test_ws_mux.py`

- [ ] **Step 1: Write the failing test** (append)

```python
class TestCreateValidationHook(unittest.TestCase):
    def test_create_operation_rejects_duplicate_routes(self):
        """create_operation must call the route validator before any
        filesystem work. We test the helper it delegates to."""
        from stack_orchestrator.deploy.deployment_create import (
            _check_http_proxy_routes,
        )

        spec = MagicMock()
        spec.get_http_proxy.return_value = [_proxy("a.example.com", [
            {"path": "/", "proxy-to": "app:8899"},
            {"path": "/", "proxy-to": "other:9000"},
        ])]
        with self.assertRaises(SystemExit):
            _check_http_proxy_routes(spec)
```

(`error_exit` raises `SystemExit` — confirm by reading its definition in `stack_orchestrator/util.py`; if it raises a different exception, assert that instead.)

- [ ] **Step 2: Run to verify failure** — ImportError for `_check_http_proxy_routes`.

- [ ] **Step 3: Implement** — in `deployment_create.py`:

```python
from stack_orchestrator.deploy.k8s.ws_mux import validate_http_proxy_routes


def _check_http_proxy_routes(parsed_spec):
    try:
        validate_http_proxy_routes(parsed_spec.get_http_proxy())
    except ValueError as e:
        error_exit(str(e))
```

And in `create_operation`, after `_check_volume_definitions(parsed_spec)` (line 979):

```python
    _check_http_proxy_routes(parsed_spec)
```

(`ws_mux.py` has no kubernetes imports, so this import is safe in the generic create path.)

- [ ] **Step 4: Run** — `python3 -m pytest tests/unit/ -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/deploy/deployment_create.py tests/unit/test_ws_mux.py
git commit -m "feat: validate http-proxy websocket routes at deploy create"
```

---

### Task 7: Integration fixture + kind verification (the spec §6 spike)

**Files:**
- Create: `stack_orchestrator/data/stacks/test-websocket/stack.yml`
- Create: `stack_orchestrator/data/compose/docker-compose-test-websocket.yml`

This task proves the controller→mux upgrade passthrough WITHOUT the old
`["h1"]` forcing, plus the two regressions the admin-API patch failed
(controller restart, redeploy). It is interactive (kind cluster) — run the
commands and check outputs; nothing here lands in CI in this plan.

- [ ] **Step 1: Create the fixture stack**

`stack_orchestrator/data/stacks/test-websocket/stack.yml`:

```yaml
version: "1.0"
name: test-websocket
description: "Fixture: http backend + ws echo backend behind a websocket: true http-proxy"
pods:
  - test-websocket
```

`stack_orchestrator/data/compose/docker-compose-test-websocket.yml`:

```yaml
services:
  backend:
    image: caddy:2-alpine
    command: ["caddy", "respond", "--listen", ":8899", "http-backend-ok"]
    ports:
      - "8899"
  wsecho:
    image: solsson/websocat
    command: ["-t", "ws-l:0.0.0.0:8900", "mirror:"]
    ports:
      - "8900"
```

(If `solsson/websocat` is unavailable, any ws echo image works — adjust the
image and keep port 8900. Verify availability first:
`docker pull solsson/websocat`.)

- [ ] **Step 2: Deploy on kind**

```bash
cd /home/dev/git_puller/repos/stack-orchestrator
SO="python3 -m stack_orchestrator.main"   # or the installed laconic-so
cat > /tmp/ws-test-spec.yml <<'EOF'
stack: test-websocket
deploy-to: k8s-kind
network:
  http-proxy:
    - host-name: wstest.localtest.me
      routes:
        - path: /
          proxy-to: backend:8899
        - path: /
          proxy-to: wsecho:8900
          websocket: true
EOF
$SO deploy --spec-file /tmp/ws-test-spec.yml create --deployment-dir /tmp/ws-test-deployment
$SO deployment --dir /tmp/ws-test-deployment start
```

Expected: create succeeds (validation passes); start prints `Created ws-mux resources` and `Created Ingress ...`.

- [ ] **Step 3: Verify objects**

```bash
KCTX=$(kubectl config current-context)
NS=$(kubectl --context $KCTX get ns -o name | grep laconic | head -1 | cut -d/ -f2)
kubectl -n $NS get deploy,svc,cm | grep ws-mux
kubectl -n $NS get ingress -o jsonpath='{.items[0].spec.rules[0].http.paths}' | python3 -m json.tool
```

Expected: one `*-ws-mux` Deployment/Service/ConfigMap; the Ingress has exactly ONE path `/` backed by the ws-mux service on 8080.

- [ ] **Step 4: The passthrough spike — HTTP and WS through the full chain**

Find the ingress entry point for kind (the caddy ingress controller's exposed port — check `kubectl -n caddy-system get svc`; on SO kind clusters port 80 is mapped to the host). Then:

```bash
# HTTP leg
curl -sS -H "Host: wstest.localtest.me" http://localhost:80/
# Expected: http-backend-ok

# WS handshake leg (the actual spike)
curl -sS -o /dev/null -w "%{http_code}\n" --http1.1 \
  -H "Host: wstest.localtest.me" \
  -H "Upgrade: websocket" -H "Connection: Upgrade" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  -H "Sec-WebSocket-Version: 13" \
  http://localhost:80/
# Expected: 101

# Full echo round-trip
docker run --rm --network host solsson/websocat \
  -t --header "Host: wstest.localtest.me" ws://localhost:80/ <<< "ping-roundtrip"
# Expected: ping-roundtrip
```

**GATE: if the handshake is not 101 here, STOP.** Capture
`kubectl -n caddy-system logs deploy/caddy-ingress-controller` and the mux
logs, and report — the fallback is the controller-fork route (spec §6), not
ad-hoc protocol forcing.

- [ ] **Step 5: Regression checks the old patch failed**

```bash
# 1. Controller restart must NOT break WS (the in-memory patch did)
kubectl -n caddy-system rollout restart deploy/caddy-ingress-controller
kubectl -n caddy-system rollout status deploy/caddy-ingress-controller
# repeat the 101 check from Step 4 — Expected: 101

# 2. Redeploy must NOT break WS (stale service FQDN did)
$SO deployment --dir /tmp/ws-test-deployment stop
$SO deployment --dir /tmp/ws-test-deployment start
# repeat the 101 check — Expected: 101

# 3. down sweeps the mux
$SO deployment --dir /tmp/ws-test-deployment stop
kubectl -n $NS get deploy,svc,cm 2>/dev/null | grep ws-mux
# Expected: no output
```

- [ ] **Step 6: Commit the fixture**

```bash
git add stack_orchestrator/data/stacks/test-websocket stack_orchestrator/data/compose/docker-compose-test-websocket.yml
git commit -m "test: websocket mux fixture stack for kind verification"
```

---

### Task 8: Docs

**Files:**
- Modify: SO's spec documentation of `network.http-proxy` — find it with `grep -rn "http-proxy" docs/ README.md` in the SO repo; add the `websocket: true` key, the pairing semantics (§3 of the design spec), and a note that SO generates a `{app}-ws-mux` per deployment with websocket routes (image overridable via `ws-mux-image`).

- [ ] **Step 1: Write the doc section** (place where the grep finds the http-proxy docs):

```markdown
#### WebSocket routes

A route may set `websocket: true` to direct WebSocket upgrade traffic at a
different backend port than plain HTTP on the same host+path (the Solana
RPC convention — wss:// at the same URL as https://):

    network:
      http-proxy:
        - host-name: rpc.example.com
          routes:
            - path: /
              proxy-to: node:8899
            - path: /
              proxy-to: node:8900
              websocket: true

Because the k8s Ingress API cannot express header-based routing, SO
generates a small Caddy mux (`{deployment}-ws-mux`) that splits on the
Upgrade header; the Ingress routes the host+path to the mux. The mux image
defaults to `caddy:2-alpine` and can be overridden with a top-level
`ws-mux-image:` spec key. At most one plain and one websocket route may
share a host+path; a websocket route without a plain twin sends all
traffic at that path to the websocket backend.
```

- [ ] **Step 2: Commit**

```bash
git add <doc files>
git commit -m "docs: document websocket: true http-proxy routes and the ws-mux"
```

---

### Task 9: Post-merge rollout (gorbagana-rpc + live) — checklist, not code

These run after SO review/merge and an SO upgrade on the RPC host; listed so nothing is dropped:

- [ ] In `/home/dev/git_puller/repos/gorbagana-rpc`: delete `stack_orchestrator/data/stacks/gorchain-rpc/deploy/patch-caddy-websocket.sh`; commit `chore: drop caddy admin-API websocket patch (SO routes websocket: true natively)`. `deployment/spec.yml` needs NO changes.
- [ ] Redeploy gorchain-rpc with the upgraded SO.
- [ ] Live verification:
  ```bash
  curl -sS -X POST https://rpc.gorbagana.wtf/ -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}'          # {"result":"ok"}
  curl -sS -o /dev/null -w "%{http_code}\n" --http1.1 \
    -H "Upgrade: websocket" -H "Connection: Upgrade" \
    -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" \
    https://rpc.gorbagana.wtf/                                  # 101
  # slotSubscribe round-trip:
  docker run --rm solsson/websocat -t wss://rpc.gorbagana.wtf/ \
    <<< '{"jsonrpc":"2.0","id":1,"method":"slotSubscribe"}'
  # Expected: {"jsonrpc":"2.0","result":<subscription id>,"id":1} then slot notifications
  ```
- [ ] Update `hyp-d34.7` (hyperlane-stacks repo): close with the verification evidence.

---

## Self-review notes

- Spec §3 semantics → Tasks 2 (validation), 3 (pairing/degenerate), 4 (ingress) ✓; §4.1→Task 2+6, §4.2→Tasks 3+4, §4.3→Task 4, §5→Tasks 2/5 (409 paths), §6→Task 7 gate ✓, §7 unit/integration/live→Tasks 1-6/7/9 ✓, §8 out of scope respected ✓.
- The exact `Spec()` constructor form (Task 1) and `opts` patching (Task 5) are flagged for the implementer to mirror from existing tests rather than guessed — both have in-repo precedents named.
- Type consistency: `collect_mux_entries(list, callable) -> list[dict]` used identically in Tasks 3 and 4; `MUX_PORT` referenced from `ws_mux` everywhere; mux naming `{app}-ws-mux`/`-config` consistent across Tasks 4, 7, 8.
