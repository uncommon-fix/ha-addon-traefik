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
#    See backend/addonkit/persist.py for why /config is the only directory that
#    survives; backend/state_sync.py holds the list of files mirrored.
#    Best-effort: `set -e` is active, and a failed mirror must not abort a boot
#    that would otherwise come up on whatever /data already has.
python3 /usr/local/bin/backend/state_sync.py restore \
    || bashio::log.warning "state restore failed; continuing with /data as found"

# 1. Migrate options.routes -> /data/routes.yml AND options.{provider,
#    cloudflare_token, acme_email, domain} -> /data/config.yml if missing.
#    migrate.py fails LOUD if options.json is unparseable so the user sees
#    the error before any silent state loss. set -e propagates the failure.
python3 /usr/local/bin/backend/migrate.py

# 2. Initial render so Traefik finds files at first start. Backend re-renders
#    on every Save thereafter. Fail-fast on bad config is desired here.
python3 /usr/local/bin/render.py

# 3. Credential export LAST. Source of truth: /data/config.yml (managed by the
#    Setup tab), falling back to the pre-multi-provider `cloudflare_token`
#    spelling and then to supervisor options.json, so an install predating the
#    provider table keeps working untouched. Mirrors render.py's precedence, so
#    what cont-init exports and what traefik.yml says always agree.
#
#    Which variables to write comes from backend/providers.py -- the same table
#    the UI and render.py use. Adding a provider does not touch this script.
#
#    Python writes the files itself rather than handing values back through a
#    shell variable: a credential that never enters the shell cannot be leaked
#    by `set -x`, a crash dump, or an accidental echo. Each file is written
#    0600, and variables belonging to OTHER providers are removed, so switching
#    provider cannot leave a stale credential for lego to pick up.
python3 - <<'PYEOF'
import json
import os
import sys

sys.path.insert(0, "/usr/local/bin")
from backend.providers import ALL_CREDENTIAL_ENV, PROVIDER_LOCAL, required_env

import yaml

ENV_DIR = "/run/s6/container_environment"


def _load(path, loader):
    try:
        with open(path) as fh:
            return loader(fh) or {}
    except Exception:
        return {}


config = _load("/data/config.yml", yaml.safe_load)
options = _load("/data/options.json", json.load)

provider = (config.get("provider") or "cloudflare").strip().lower()

creds = dict(config.get("provider_credentials") or {})
# Legacy single-token spelling, still honoured for installs that predate the
# provider table (and for options.json, which is older still).
for src in (config, options):
    legacy = (src.get("cloudflare_token") or "").strip()
    if legacy and not (creds.get("CF_DNS_API_TOKEN") or "").strip():
        creds["CF_DNS_API_TOKEN"] = legacy

wanted = {} if provider == PROVIDER_LOCAL else {
    env: (creds.get(env) or "").strip()
    for env in required_env(provider)
}

os.makedirs(ENV_DIR, exist_ok=True)
written, missing = [], []

for env in sorted(ALL_CREDENTIAL_ENV):
    path = os.path.join(ENV_DIR, env)
    value = wanted.get(env, "")
    if value:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)          # no trailing newline: it corrupts the value
        written.append(env)
    else:
        # Not ours this time round. Remove rather than leave stale.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        if env in wanted:
            missing.append(env)

# Names only. NEVER a value.
if provider == PROVIDER_LOCAL:
    print("provider 'local': no DNS credentials needed", file=sys.stderr)
elif missing:
    print(
        f"provider {provider!r}: missing {missing} - ACME cannot run until "
        "these are set in the Setup tab",
        file=sys.stderr,
    )
else:
    print(f"provider {provider!r}: exported {written}", file=sys.stderr)
PYEOF

bashio::log.info "DNS credentials exported for the configured provider"
