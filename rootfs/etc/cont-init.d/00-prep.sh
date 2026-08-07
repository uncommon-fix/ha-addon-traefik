#!/usr/bin/with-contenv bashio
# s6-overlay v3 paths: container_environment is /run/s6/container_environment/
# (v2 used /var/run/s6/...; if base ever drops the back-compat symlink, this
# path stays correct).
set -euo pipefail

bashio::log.info "traefik add-on cont-init: migrate + initial render + token export"

# 0. Repopulate /data from the /config mirror when this is a fresh install that
#    still has the previous install's config. The supervisor deletes /data on
#    every uninstall unconditionally, so without this a reinstall starts blank
#    even when the user chose to keep the configuration. No-ops when /data
#    already holds state, so a normal restart is untouched.
#    See rootfs/usr/local/bin/state-sync.sh for why /config is the only
#    directory that survives, and docs/state-persistence.md for the contract.
/usr/local/bin/state-sync.sh restore

# 1. Migrate options.routes -> /data/routes.yml AND options.{provider,
#    cloudflare_token, acme_email, domain} -> /data/config.yml if missing.
#    migrate.py fails LOUD if options.json is unparseable so the user sees
#    the error before any silent state loss. set -e propagates the failure.
python3 /usr/local/bin/backend/migrate.py

# 2. Initial render so Traefik finds files at first start. Backend re-renders
#    on every Save thereafter. Fail-fast on bad config is desired here.
python3 /usr/local/bin/render.py

# 3. Token export LAST. Source-of-truth: /data/config.yml (managed by the
#    add-on's Setup tab). Fallback to supervisor options.cloudflare_token
#    for Phase A-C back-compat. Mirrors render.py / backend's
#    _load_core_config precedence so cont-init env and rendered traefik.yml
#    always agree.
#
#    Write with umask 077 so the file is mode 600 (root-only).
#    printf '%s' strips any trailing newline that would otherwise corrupt
#    the env-var-based CF API auth (Phase C SC2155 lesson).
TOKEN="$(python3 - <<'PYEOF'
import json, os, sys
import yaml

token = ""

config_yml = "/data/config.yml"
if os.path.exists(config_yml):
    try:
        data = yaml.safe_load(open(config_yml)) or {}
        token = (data.get("cloudflare_token") or "").strip()
    except Exception:
        token = ""

if not token:
    options_json = "/data/options.json"
    if os.path.exists(options_json):
        try:
            data = json.load(open(options_json))
            token = (data.get("cloudflare_token") or "").strip()
        except Exception:
            token = ""

sys.stdout.write(token)
PYEOF
)"

if [ -n "$TOKEN" ]; then
    (
        umask 077
        printf '%s' "$TOKEN" \
            > /run/s6/container_environment/CF_DNS_API_TOKEN
    )
    unset TOKEN
    bashio::log.info "cloudflare DNS-01 token loaded; ACME resolver active"
fi
