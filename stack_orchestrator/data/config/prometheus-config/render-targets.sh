#!/bin/sh
# Prometheus container entrypoint: render the scrape config and file_sd target
# files from env, then exec prometheus. This runs on every container start, so
# the targets and scheme are regenerated on each (re)start — adding a
# validator/relayer is an env change + restart, with no deploy hook or
# laconic-so update step.
#
# /etc/prometheus is a read-only ConfigMap mount, so rendered output is written
# under the writable data dir (/prometheus); prometheus.yml's file_sd_configs
# point there.
set -eu

RENDER_DIR=/prometheus
TARGETS_DIR="$RENDER_DIR/targets"
mkdir -p "$TARGETS_DIR"

# render_targets <comma-separated instance=host:port list> <output file>
render_targets() {
    out=$2
    : > "$out"
    oldifs=$IFS
    IFS=','
    for entry in $1; do
        IFS=$oldifs
        entry=$(printf '%s' "$entry" | tr -d ' ')
        if [ -n "$entry" ]; then
            case $entry in
                *=*:*) : ;;
                *)
                    echo "render-targets: bad entry '$entry' (want instance=host:port)" >&2
                    exit 1
                    ;;
            esac
            printf -- '- targets: ["%s"]\n  labels: { hyperlane_instance: %s }\n' \
                "${entry#*=}" "${entry%%=*}" >> "$out"
        fi
        IFS=','
    done
    IFS=$oldifs
    [ -s "$out" ] || printf '[]\n' > "$out"
}

render_targets "${PROMETHEUS_VALIDATOR_TARGETS:-}" "$TARGETS_DIR/validators.yml"
render_targets "${PROMETHEUS_RELAYER_TARGETS:-}" "$TARGETS_DIR/relayer.yml"

# Scrape scheme: https in prod (Caddy + Let's Encrypt); the e2e harness sets
# http to scrape pods in-cluster. Rendered into a writable copy of the config.
scheme=${PROMETHEUS_SCRAPE_SCHEME:-https}
sed "s|scheme: https|scheme: ${scheme}|" /etc/prometheus/prometheus.yml > "$RENDER_DIR/prometheus.yml"

exec /bin/prometheus --config.file="$RENDER_DIR/prometheus.yml" "$@"
