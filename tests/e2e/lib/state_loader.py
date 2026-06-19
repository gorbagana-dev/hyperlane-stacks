"""Loads deployer-generated state files into consumer stack deploy_dirs.

The deployer Jobs write JSON files (and one multi-file directory: registry/)
to STATE_OUTPUT_DIR. Before each consumer stack runs `deployment start`, the
loader copies the relevant subset of state files into
{deploy_dir}/configmaps/<cm-name>/ — which SO then turns into k8s ConfigMaps
that the consumer pod mounts as normal volumes.

Hardcoded mapping below is the source of truth for consumer↔state coupling.
If a state file the loader expects to copy is missing, populate() exits with
a clear error before the consumer Job/Pod is started.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# Only stacks whose compose actually mounts a CM appear here. Stacks that
# consume deployer state via env-var injection (gas-oracle, monitoring)
# read individual values through BridgeStateLoader.read_json in conftest
# spec-patching — they don't need populate() to copy files.
CONSUMER_STATE_FILES: dict[str, list[tuple[str, str]]] = {
    "hyperlane-validator": [
        ("agent-config.json", "agent-config"),
    ],
    "hyperlane-relayer": [
        ("agent-config.json", "agent-config"),
    ],
    # Env-var consumers and stacks that don't read deployer state at all:
    "hyperlane-svm-deployer": [],
    "hyperlane-svm-warp-deployer": [],   # reads /state at runtime via mount
    "hyperlane-minio": [],
    "hyperlane-gas-oracle": [],          # env-var injection via read_json
    "hyperlane-monitoring": [],          # env-var injection via read_json
    # warp-ui mounts the deployer-built warpRoutes.yaml as the warp-ui-config CM:
    "hyperlane-warp-ui": [
        ("warp-routes/warpRoutes.yaml", "warp-ui-config"),
    ],
    # explorer's scraper reads agent-config.json (mailboxes, domain ids); its frontend
    # reads warpRoutes.yaml (warp-route injection) from the same ConfigMap:
    "hyperlane-explorer": [
        ("agent-config.json", "agent-config"),
        ("warp-routes/warpRoutes.yaml", "agent-config"),
    ],
}


class BridgeStateLoader:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def expected_files_for(self, stack_name: str) -> list[str]:
        if stack_name not in CONSUMER_STATE_FILES:
            raise KeyError(
                f"BridgeStateLoader: unknown stack {stack_name!r}. "
                f"Known stacks: {sorted(CONSUMER_STATE_FILES)}"
            )
        return [src for src, _cm in CONSUMER_STATE_FILES[stack_name]]

    def assert_present(self, stack_name: str) -> None:
        missing = [
            src
            for src in self.expected_files_for(stack_name)
            if not (self.state_dir / src).exists()
        ]
        if missing:
            raise FileNotFoundError(
                f"BridgeStateLoader: missing state files for "
                f"{stack_name!r} under {self.state_dir}: {missing}"
            )

    def populate(self, stack_name: str, deploy_dir: Path) -> None:
        """Copy state files into {deploy_dir}/configmaps/<cm-name>/.

        Called before `laconic-so deployment start` for the given consumer.
        SO then creates one k8s ConfigMap per <cm-name>.
        """
        self.assert_present(stack_name)
        for src_rel, cm_name in CONSUMER_STATE_FILES[stack_name]:
            src = self.state_dir / src_rel
            dst_dir = deploy_dir / "configmaps" / cm_name
            dst_dir.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                # Multi-file CM: copy each top-level file (SO doesn't
                # recurse subdirs into ConfigMaps; flat layout only).
                for f in src.iterdir():
                    if f.is_file():
                        shutil.copy2(f, dst_dir / f.name)
            else:
                shutil.copy2(src, dst_dir / src.name)

    def read_json(self, file_rel: str) -> dict:
        """Read a state JSON file. Used by conftest to patch test-spec
        env vars (REPLACE_AT_RUNTIME) from scalar state values
        (gas-oracle, warp-ui mailboxes, monitoring's balance-monitor).
        """
        path = self.state_dir / file_rel
        if not path.exists():
            raise FileNotFoundError(
                f"BridgeStateLoader: required state file {file_rel} not at {path}"
            )
        return json.loads(path.read_text())

    def read_program_ids(self, chain: str) -> dict:
        """Convenience: program-ids.json's `<chain>` key as a dict."""
        ids = self.read_json("program-ids.json")
        if chain not in ids:
            raise KeyError(
                f"program-ids.json missing chain {chain!r}; keys={list(ids)}"
            )
        return ids[chain]

    def discover_routes(self) -> list[str]:
        """Route directory names under <state>/warp-routes/ (per-route layout
        written by deploy.sh). Empty list when no warp-routes/ dir exists."""
        base = self.state_dir / "warp-routes"
        if not base.is_dir():
            return []
        return [d.name for d in base.iterdir() if d.is_dir()]

    def read_route_token_config(self, route: str) -> dict:
        """Parsed token-config.json for a route under warp-routes/<route>/."""
        return self.read_json(str(Path("warp-routes") / route / "token-config.json"))

    def read_route_program_addresses(self, route: str) -> dict[str, str]:
        """`{chain: base58}` from every file in a route's warp-deploy-outputs/,
        taking each top-level chain entry's `base58` value."""
        outputs = self.state_dir / "warp-routes" / route / "warp-deploy-outputs"
        if not outputs.is_dir():
            raise FileNotFoundError(f"{outputs} does not exist")
        programs: dict[str, str] = {}
        for f in outputs.iterdir():
            if f.is_file():
                for chain, entry in json.loads(f.read_text()).items():
                    if isinstance(entry, dict) and entry.get("base58"):
                        programs[chain] = entry["base58"]
        return programs
