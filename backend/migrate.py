#!/usr/bin/env python3
"""One-time migrations from supervisor options.json to /data/{routes,config}.yml.

Migrates routes and core config (provider, cloudflare_token, acme_email,
domain) into /data/*.yml. These become managed by the add-on's own UI; the
supervisor schema retains the fields as vestigial back-compat (removing schema
fields with orphaned saved-options keys breaks `ha apps update`).

Failure semantics:
- target file exists -> idempotent no-op for that target.
- /data/options.json MISSING -> fresh install; write empty defaults, return 0.
- /data/options.json UNPARSEABLE -> return 1 (REFUSE to write empty targets,
  which would silently destroy user state on the next save).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

DATA = Path("/data")
ROUTES_YML = DATA / "routes.yml"
CONFIG_YML = DATA / "config.yml"
MIDDLEWARES_YML = DATA / "middlewares.yml"
OPTIONS = DATA / "options.json"

# Fields that move from supervisor options into /data/config.yml in 0.5.0.
# Order preserved so the YAML is human-readable.
CORE_CONFIG_FIELDS = ["provider", "cloudflare_token", "acme_email", "domain"]


def _dump(data: dict) -> str:
    return yaml.safe_dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=False,
        width=4096,
    )


def _write(target: Path, data: dict, *, label: str) -> int:
    try:
        target.write_text(_dump(data))
    except OSError as e:
        print(
            f"migrate: REFUSING to continue -- cannot write {target}: {e}",
            file=sys.stderr,
        )
        return 1
    print(f"migrate: wrote {label} to {target}", file=sys.stderr)
    return 0


def _empty_config() -> dict:
    # ha_hostname: subdomain for the auto-injected route to this HA instance.
    # "hass" -> hass.<domain> -> homeassistant:8123. Empty disables the route.
    # entrypoint_http/https: Traefik entryPoint names referenced from routes.
    # log_level: Traefik log level.
    return {
        "provider": "cloudflare",
        "cloudflare_token": "",
        "acme_email": "",
        "domain": "",
        "ha_hostname": "hass",
        "entrypoint_http": "web",
        "entrypoint_https": "websecure",
        "log_level": "INFO",
    }


def _migrate_routes(opts: dict | None) -> int:
    if ROUTES_YML.exists():
        print("migrate: /data/routes.yml exists; nothing to do", file=sys.stderr)
        return 0
    routes = (opts or {}).get("routes") or []
    return _write(ROUTES_YML, {"routes": routes}, label=f"{len(routes)} routes")


def _migrate_config(opts: dict | None) -> int:
    if CONFIG_YML.exists():
        print("migrate: /data/config.yml exists; nothing to do", file=sys.stderr)
        return 0
    # Map supervisor options field-by-field. acme_resolver in options maps to
    # provider in config.yml (Traefik resolver name is derived from provider).
    src = opts or {}
    data = _empty_config()
    if src.get("acme_resolver"):
        data["provider"] = src["acme_resolver"]
    for field in ("cloudflare_token", "acme_email", "domain", "ha_hostname",
                  "entrypoint_http", "entrypoint_https", "log_level"):
        if src.get(field):
            data[field] = src[field]
    return _write(CONFIG_YML, data, label="core config")


def _ensure_ha_system_route() -> None:
    """Phase F (0.7.0): the HA route stops being a renderer-injected ghost and
    becomes a real, persisted route in /data/routes.yml marked with
    system: ha_self. Idempotent: only seeds the route if no ha_self entry
    already exists.

    ha_hostname truth table:
      config.yml absent              -> default "hass", enabled=True
      config.yml present, key absent -> default "hass", enabled=True
      config.yml present, key empty  -> hostname="hass", enabled=False (user disabled)
      config.yml present, key set    -> use the value, enabled=True
    """
    if not ROUTES_YML.exists():
        routes: list = []
    else:
        parsed = yaml.safe_load(ROUTES_YML.read_text()) or {}
        routes = parsed.get("routes") or []
    if any(r.get("system") == "ha_self" for r in routes):
        return  # idempotent steady state

    ha_hostname = "hass"
    enabled = True
    if CONFIG_YML.exists():
        cfg = yaml.safe_load(CONFIG_YML.read_text()) or {}
        if "ha_hostname" in cfg:
            v = (cfg["ha_hostname"] or "").strip()
            if v:
                ha_hostname = v
            else:
                enabled = False  # explicit empty in old config = user disabled

    system_route = {
        "system": "ha_self",
        "hostname": ha_hostname,
        "backend_kind": "home_assistant",
        "backend_host": None,
        "backend_port": None,
        "scheme": "http",
        "tls": True,
        "enabled": enabled,
        "middlewares": [],
        "health_path": None,
    }
    routes.insert(0, system_route)  # system routes at front
    ROUTES_YML.write_text(_dump({"routes": routes}))
    print(
        f"migrate: seeded HA system route "
        f"(hostname={ha_hostname!r}, enabled={enabled})",
        file=sys.stderr,
    )


def _ensure_middlewares_file() -> None:
    """Phase F (0.7.0): bootstrap /data/middlewares.yml with the versioned
    empty shape so the backend's PUT /api/middlewares has a target to write
    and the renderer's _load_middlewares has a parseable file to read."""
    if MIDDLEWARES_YML.exists():
        return
    MIDDLEWARES_YML.write_text(_dump({"version": 1, "middlewares": []}))
    print("migrate: wrote empty /data/middlewares.yml", file=sys.stderr)


def main() -> int:
    # Phase A-E bootstrap: routes + config from options.json (or empty defaults).
    bootstrap_rc = 0
    if not OPTIONS.exists():
        print(
            "migrate: /data/options.json missing (fresh install); "
            "writing empty defaults",
            file=sys.stderr,
        )
        if not ROUTES_YML.exists():
            bootstrap_rc = _write(ROUTES_YML, {"routes": []}, label="empty routes") or bootstrap_rc
        if not CONFIG_YML.exists():
            bootstrap_rc = _write(CONFIG_YML, _empty_config(), label="empty config") or bootstrap_rc
    else:
        try:
            opts = json.loads(OPTIONS.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"migrate: REFUSING to write empty targets -- "
                f"options.json unreadable: {e}",
                file=sys.stderr,
            )
            return 1
        bootstrap_rc = _migrate_routes(opts) or bootstrap_rc
        bootstrap_rc = _migrate_config(opts) or bootstrap_rc

    # These bootstrap steps run unconditionally, each exception-isolated so a
    # single failure doesn't block the other; they must not be skipped by the
    # early-return on missing options.json.
    bootstrap_step_rc = 0
    try:
        _ensure_ha_system_route()
    except Exception as e:
        print(f"migrate: HA system route step failed: {e}", file=sys.stderr)
        bootstrap_step_rc = 1
    try:
        _ensure_middlewares_file()
    except Exception as e:
        print(f"migrate: middlewares step failed: {e}", file=sys.stderr)
        bootstrap_step_rc = 1

    return bootstrap_rc or bootstrap_step_rc


if __name__ == "__main__":
    sys.exit(main())
