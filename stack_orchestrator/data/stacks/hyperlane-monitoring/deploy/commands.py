from stack_orchestrator.deploy.deployment_context import DeploymentContext

# Spec config var -> rendered file_sd file consumed by prometheus.yml.
TARGET_FILES = (
    ("PROMETHEUS_VALIDATOR_TARGETS", "validators.yml"),
    ("PROMETHEUS_RELAYER_TARGETS", "relayer.yml"),
)


def _parse_targets(raw, var_name):
    """Parse a comma-separated 'instance=host:port' target list."""
    targets = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(
                f"{var_name}: entry '{entry}' is not 'instance=host:port'"
            )
        instance, hostport = (part.strip() for part in entry.split("=", 1))
        if ":" not in hostport:
            raise ValueError(
                f"{var_name}: target '{hostport}' is not 'host:port'"
            )
        targets.append((instance, hostport))
    return targets


def _render_file_sd(targets):
    """Render Prometheus file_sd YAML, tagging each target with its instance."""
    if not targets:
        return "[]\n"
    lines = []
    for instance, hostport in targets:
        lines.append(f'- targets: ["{hostport}"]')
        lines.append(f"  labels: {{ hyperlane_instance: {instance} }}")
    return "\n".join(lines) + "\n"


def create(context: DeploymentContext, extra_args):
    """Render Prometheus file_sd target files from the deployment spec.

    The validator/relayer scrape targets live in the spec's config: block
    (PROMETHEUS_VALIDATOR_TARGETS / PROMETHEUS_RELAYER_TARGETS) as
    comma-separated 'instance=host:port' entries. Prometheus watches the
    rendered files via file_sd_configs and reloads them without a restart, so
    targets can be added to a live deployment by re-rendering this ConfigMap.
    """
    config = context.spec.get("config", {}) or {}
    cm_dir = context.deployment_dir / "configmaps" / "prometheus-config"

    for var_name, filename in TARGET_FILES:
        targets = _parse_targets(config.get(var_name, ""), var_name)
        (cm_dir / filename).write_text(_render_file_sd(targets))
        print(f"Rendered {len(targets)} scrape target(s) to {filename}")
