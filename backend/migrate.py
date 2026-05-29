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
import os
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

# Default HTTP->HTTPS redirect middleware, seeded + attached to the HA system
# route so http://<domain> 308-redirects to https:// out of the box.
REDIRECT_MW_NAME = "redirect-to-https"
REDIRECT_MW = {
    "name": REDIRECT_MW_NAME,
    "type": "redirectScheme",
    "config": {"scheme": "https", "permanent": True},
}

# alpha.14: skip-tls-verify is no longer a middleware. It lives on the route
# as a bool (`skip_tls_verify`); render.py reads it directly to apply the
# service-level insecureSkipVerify transport. The one-shot strip migration
# (_strip_skip_tls_middleware) cleans up pre-alpha.14 state on first boot.

# Pre-alpha.14 name of the synthetic middleware that this release demotes.
LEGACY_SKIP_TLS_MW_NAME = "skip-tls-verify"

# name -> canonical type for every add-on-managed built-in.
BUILTIN_MIDDLEWARES = [REDIRECT_MW]


def _dump(data: dict) -> str:
    return yaml.safe_dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=False,
        width=4096,
    )


def _atomic_write(target: Path, payload: str) -> None:
    """Crash-safe YAML write: tmp + flush + fsync + atomic rename + parent fsync.
    Mirrors server.py's _atomic_write_bytes/_atomic_write_yml so an interrupted
    migrate.py can't leave a torn /data/*.yml that bricks the next boot."""
    tmp = target.parent / (target.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    fd = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(target: Path, data: dict, *, label: str) -> int:
    try:
        _atomic_write(target, _dump(data))
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
        "force_ssl": False,
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
    _atomic_write(ROUTES_YML, _dump({"routes": routes}))
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
    _atomic_write(MIDDLEWARES_YML, _dump({"version": 1, "middlewares": []}))
    print("migrate: wrote empty /data/middlewares.yml", file=sys.stderr)


def _ensure_builtin_middlewares() -> None:
    """alpha.7 / alpha.14: reconcile-always — ensure every add-on-managed
    built-in exists in /data/middlewares.yml with its canonical type.
    Idempotent and runs on every start (no one-shot marker): built-ins are
    tools the add-on owns, so a missing one (fresh install, or a pre-alpha.7
    install) is seeded, and a wrong-typed one is fixed in place (user config
    preserved). Does NOT attach anything to a route — redirect-to-https's
    one-time ha_self attachment stays in _ensure_redirect_middleware().

    alpha.14: list collapsed to redirect-to-https only; skip-tls-verify was
    demoted to a per-route bool. _strip_skip_tls_middleware handles the
    one-shot data migration."""
    if MIDDLEWARES_YML.exists():
        doc = yaml.safe_load(MIDDLEWARES_YML.read_text()) or {}
    else:
        doc = {"version": 1, "middlewares": []}
    mws = doc.get("middlewares") or []
    by_name = {m.get("name"): m for m in mws}
    changed = False
    for builtin in BUILTIN_MIDDLEWARES:
        name = builtin["name"]
        existing = by_name.get(name)
        if existing is None:
            mws.append(dict(builtin))
            changed = True
            print(f"migrate: seeded built-in middleware {name!r}", file=sys.stderr)
        elif existing.get("type") != builtin["type"]:
            existing["type"] = builtin["type"]
            if existing.get("config") is None:
                existing["config"] = dict(builtin["config"])
            changed = True
            print(
                f"migrate: fixed built-in middleware {name!r} type "
                f"-> {builtin['type']}",
                file=sys.stderr,
            )
    if changed:
        doc["middlewares"] = mws
        doc.setdefault("version", 1)
        _atomic_write(MIDDLEWARES_YML, _dump(doc))


def _ensure_redirect_middleware() -> None:
    """Idempotently seed the redirect-to-https middleware and attach it to the
    HA system route. Runs unconditionally so EXISTING installs get back-filled,
    but only once: a `redirect_seeded` marker in config.yml stops it from
    resurrecting a middleware/attachment the user later removed on purpose."""
    cfg: dict = {}
    if CONFIG_YML.exists():
        cfg = yaml.safe_load(CONFIG_YML.read_text()) or {}
    if cfg.get("redirect_seeded"):
        return

    # 1. Ensure the middleware exists in /data/middlewares.yml (by name).
    if MIDDLEWARES_YML.exists():
        mw_doc = yaml.safe_load(MIDDLEWARES_YML.read_text()) or {}
    else:
        mw_doc = {"version": 1, "middlewares": []}
    mws = mw_doc.get("middlewares") or []
    if not any(m.get("name") == REDIRECT_MW_NAME for m in mws):
        mws.append(dict(REDIRECT_MW))
        mw_doc["middlewares"] = mws
        _atomic_write(MIDDLEWARES_YML, _dump(mw_doc))
        print(f"migrate: seeded {REDIRECT_MW_NAME!r} middleware", file=sys.stderr)

    # 2. Attach it to the HA system route only (never user routes).
    if ROUTES_YML.exists():
        routes_doc = yaml.safe_load(ROUTES_YML.read_text()) or {}
        routes = routes_doc.get("routes") or []
        changed = False
        for r in routes:
            if r.get("system") == "ha_self":
                r_mws = r.get("middlewares") or []
                if REDIRECT_MW_NAME not in r_mws:
                    r_mws.append(REDIRECT_MW_NAME)
                    r["middlewares"] = r_mws
                    changed = True
        if changed:
            routes_doc["routes"] = routes
            _atomic_write(ROUTES_YML, _dump(routes_doc))
            print(
                "migrate: attached redirect-to-https to HA system route",
                file=sys.stderr,
            )

    # 3. One-shot marker (config.yml already exists by this point in main()).
    cfg["redirect_seeded"] = True
    _atomic_write(CONFIG_YML, _dump(cfg))


def _strip_skip_tls_middleware() -> None:
    """alpha.14: demote `skip-tls-verify` from a synthetic middleware to a
    per-route bool. Two steps, both atomic and idempotent:

    1. Walk /data/routes.yml: for every route with `'skip-tls-verify'` in
       middlewares, set `skip_tls_verify=True` and strip the magic string
       from the list. Also backfill `skip_tls_verify: False` on every route
       missing the key. Without this backfill the LOCKED-set check in
       `_enforce_system_route_protection` compares `None` (on-disk missing
       key) vs `False` (UI-sent default) and rejects every system-route PUT.
    2. Walk /data/middlewares.yml: drop the `skip-tls-verify` entry.

    No marker: the steady-state cost on a clean install is two yaml loads
    that find nothing to change (no write, no log). The previous one-shot
    marker design was over-engineering — and risky, because if alpha.13→14
    set the marker BEFORE the backfill landed (as happened during dev), real
    upgraders would have routes missing the bool and the LOCKED check would
    bite them. Always running ensures convergence.
    """
    # Step 1: routes.yml — set the bool, strip the string, backfill defaults.
    if ROUTES_YML.exists():
        routes_doc = yaml.safe_load(ROUTES_YML.read_text()) or {}
        routes = routes_doc.get("routes") or []
        routes_changed = False
        stripped = 0
        for r in routes:
            mws = r.get("middlewares") or []
            had_legacy = LEGACY_SKIP_TLS_MW_NAME in mws
            if had_legacy:
                r["middlewares"] = [m for m in mws if m != LEGACY_SKIP_TLS_MW_NAME]
                r["skip_tls_verify"] = True
                routes_changed = True
                stripped += 1
            elif "skip_tls_verify" not in r:
                r["skip_tls_verify"] = False
                routes_changed = True
        if routes_changed:
            routes_doc["routes"] = routes
            _atomic_write(ROUTES_YML, _dump(routes_doc))
            if stripped:
                print(
                    f"migrate: alpha.14 — moved skip-tls-verify to "
                    f"route.skip_tls_verify on {stripped} route(s); "
                    "backfilled the field on the rest",
                    file=sys.stderr,
                )
            else:
                print(
                    "migrate: alpha.14 — backfilled "
                    "route.skip_tls_verify=False on routes missing the field",
                    file=sys.stderr,
                )

    # Step 2: middlewares.yml — drop the entry if present.
    if MIDDLEWARES_YML.exists():
        mw_doc = yaml.safe_load(MIDDLEWARES_YML.read_text()) or {}
        mws = mw_doc.get("middlewares") or []
        new_mws = [m for m in mws if m.get("name") != LEGACY_SKIP_TLS_MW_NAME]
        if len(new_mws) != len(mws):
            mw_doc["middlewares"] = new_mws
            _atomic_write(MIDDLEWARES_YML, _dump(mw_doc))
            print(
                "migrate: alpha.14 — removed skip-tls-verify from "
                "/data/middlewares.yml (now a per-route bool)",
                file=sys.stderr,
            )


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
    try:
        _ensure_builtin_middlewares()
    except Exception as e:
        print(f"migrate: built-in middlewares step failed: {e}", file=sys.stderr)
        bootstrap_step_rc = 1
    try:
        _ensure_redirect_middleware()
    except Exception as e:
        print(f"migrate: redirect middleware step failed: {e}", file=sys.stderr)
        bootstrap_step_rc = 1
    try:
        _strip_skip_tls_middleware()
    except Exception as e:
        print(f"migrate: skip-tls-verify migration failed: {e}", file=sys.stderr)
        bootstrap_step_rc = 1

    return bootstrap_rc or bootstrap_step_rc


if __name__ == "__main__":
    sys.exit(main())
