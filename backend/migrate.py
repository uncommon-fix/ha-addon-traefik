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
import uuid
from pathlib import Path
from typing import Any

import yaml

DATA = Path("/data")
ROUTES_YML = DATA / "routes.yml"
CONFIG_YML = DATA / "config.yml"
MIDDLEWARES_YML = DATA / "middlewares.yml"
OPTIONS = DATA / "options.json"

# alpha.20: draft/live split. The renderer reads ONLY *.yml; the editor
# mutates *.draft.yml. POST /api/apply copies draft → live atomically
# (journal-based) and runs render.
ROUTES_DRAFT_YML = DATA / "routes.draft.yml"
CONFIG_DRAFT_YML = DATA / "config.draft.yml"
MIDDLEWARES_DRAFT_YML = DATA / "middlewares.draft.yml"

# alpha.20: baseline files = exact bytes of LIVE at the moment draft was
# last initialized / last Apply'd. Drives the 3-way merge on live drift
# (when migrate.py mutates live behind the editor's back). Storing
# content (not just hash) so the merge can distinguish "user edited" vs
# "migration edited" without losing the common ancestor.
ROUTES_BASELINE_YML = DATA / ".routes.baseline.yml"
CONFIG_BASELINE_YML = DATA / ".config.baseline.yml"
MIDDLEWARES_BASELINE_YML = DATA / ".middlewares.baseline.yml"

# alpha.20: per-surface live + draft + baseline triples.
SURFACE_TRIPLES = [
    (ROUTES_YML, ROUTES_DRAFT_YML, ROUTES_BASELINE_YML),
    (MIDDLEWARES_YML, MIDDLEWARES_DRAFT_YML, MIDDLEWARES_BASELINE_YML),
    (CONFIG_YML, CONFIG_DRAFT_YML, CONFIG_BASELINE_YML),
]

# alpha.20: journal marker written by POST /api/apply BEFORE the live
# rename. cont-init / migrate completes any pending apply on next boot.
APPLY_JOURNAL = DATA / ".apply_journal.yml"

# alpha.20: surfaced to the frontend on next load if the 3-way merge
# couldn't reconcile (user and migration both edited the same field).
DRAFT_RESET_REASONS = DATA / ".draft_reset_reasons.json"

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


def _strip_route_tags() -> None:
    """alpha.19: tags removal. The alpha.16 `tags: list[str]` field is gone
    from the schema; walk routes.yml and drop the key if present so the
    next save round-trip stays clean. Idempotent + cheap on steady state:
    one yaml load, possibly no write. No marker (same shape as the
    alpha.14 _strip_skip_tls_middleware walk — markers are a foot-gun
    if they land before the cleanup matures).
    """
    if not ROUTES_YML.exists():
        return
    routes_doc = yaml.safe_load(ROUTES_YML.read_text()) or {}
    routes = routes_doc.get("routes") or []
    changed = False
    stripped = 0
    for r in routes:
        if "tags" in r:
            del r["tags"]
            changed = True
            stripped += 1
    if changed:
        routes_doc["routes"] = routes
        _atomic_write(ROUTES_YML, _dump(routes_doc))
        print(
            f"migrate: alpha.19 — stripped route.tags from {stripped} "
            "route(s) (field removed from schema)",
            file=sys.stderr,
        )


def _recover_apply_journal() -> None:
    """alpha.20: complete-or-rollback a pending POST /api/apply that crashed.

    POST /api/apply writes the journal AFTER staging all three drafts as
    `*.applying` siblings, then atomically renames each `*.applying` → live,
    then deletes the journal. If the addon was killed between staging and
    journal write → no `*.applying` files (they were leftover from a prior
    failed attempt and `_atomic_write_bytes` already replaced them). If
    killed between journal write and ALL renames complete → some live files
    point at old content, some at new; recover by completing every rename
    listed in the journal. If killed AFTER all renames but before journal
    delete → no `.applying` files remain; we just need to delete the journal.

    Idempotent: missing journal → no-op. Run as the VERY FIRST step so all
    subsequent migration steps see a consistent live.
    """
    if not APPLY_JOURNAL.exists():
        return
    try:
        journal = yaml.safe_load(APPLY_JOURNAL.read_text()) or {}
    except yaml.YAMLError as e:
        print(
            f"migrate: apply journal unreadable ({e}); leaving it in place "
            "for inspection. WARNING: live config may be inconsistent.",
            file=sys.stderr,
        )
        return
    targets = journal.get("targets") or []
    completed = 0
    for target_str in targets:
        target = Path(target_str)
        applying = target.parent / (target.name + ".applying")
        if applying.exists():
            os.replace(str(applying), str(target))
            completed += 1
    try:
        APPLY_JOURNAL.unlink()
    except FileNotFoundError:
        pass
    print(
        f"migrate: alpha.20 — apply journal recovered "
        f"({completed} pending rename(s) completed of {len(targets)})",
        file=sys.stderr,
    )


def _backfill_route_rid() -> None:
    """alpha.20: assign uuid4 `rid` to any live route lacking one. Idempotent.

    MUST run AFTER `_ensure_ha_system_route` (which seeds the HA self-route
    on first boot without a rid) so the seeded route also gets a rid on
    first boot — otherwise the diff endpoint sees it as added+removed
    until the next boot.
    """
    if not ROUTES_YML.exists():
        return
    routes_doc = yaml.safe_load(ROUTES_YML.read_text()) or {}
    routes = routes_doc.get("routes") or []
    changed = False
    assigned = 0
    for r in routes:
        if not isinstance(r, dict):
            continue
        if not r.get("rid"):
            r["rid"] = str(uuid.uuid4())
            changed = True
            assigned += 1
    if changed:
        routes_doc["routes"] = routes
        _atomic_write(ROUTES_YML, _dump(routes_doc))
        print(
            f"migrate: alpha.20 — assigned rid to {assigned} route(s)",
            file=sys.stderr,
        )


def _backfill_middleware_mid() -> None:
    """alpha.20: assign uuid4 `mid` to any live middleware lacking one.

    Idempotent. Stable identity for the per-row change-tracking UI; needed
    because middleware NAMES are user-editable (server.py:495-507 only
    enforces regex + uniqueness), so a name rename would otherwise show as
    delete+add in the pending changes summary. Runs AFTER
    `_ensure_builtin_middlewares` so the seeded built-ins also get a mid.
    """
    if not MIDDLEWARES_YML.exists():
        return
    mw_doc = yaml.safe_load(MIDDLEWARES_YML.read_text()) or {}
    mws = mw_doc.get("middlewares") or []
    changed = False
    assigned = 0
    for m in mws:
        if not isinstance(m, dict):
            continue
        if not m.get("mid"):
            m["mid"] = str(uuid.uuid4())
            changed = True
            assigned += 1
    if changed:
        mw_doc["middlewares"] = mws
        _atomic_write(MIDDLEWARES_YML, _dump(mw_doc))
        print(
            f"migrate: alpha.20 — assigned mid to {assigned} middleware(s)",
            file=sys.stderr,
        )


def _routes_by_rid(doc: dict) -> dict[str, dict]:
    """{rid: route_dict} for a routes.yml doc; routes lacking rid are skipped
    (`_backfill_route_rid` runs first so this is robust to fresh-install
    state)."""
    return {r["rid"]: r for r in (doc.get("routes") or [])
            if isinstance(r, dict) and r.get("rid")}


def _mws_by_mid(doc: dict) -> dict[str, dict]:
    return {m["mid"]: m for m in (doc.get("middlewares") or [])
            if isinstance(m, dict) and m.get("mid")}


def _merge_3way_rid_collection(prior: dict, draft: dict, current: dict,
                                surface: str, key_field: str) -> tuple[list, list]:
    """3-way merge of rid/mid-keyed entries. Returns (merged_list, conflicts).

    For each unit (route by rid OR middleware by mid):
      - in current only           -> added by migration; merge into draft
      - in prior only             -> removed by migration; remove from draft
                                     unless the user has edited it (conflict)
      - in all three, draft == prior, current differs -> migration touched a
        field the user hadn't touched; take current (silent merge)
      - in all three, draft differs, current == prior -> user touched; keep draft
      - in all three, both differ same way -> coincidence; keep current
      - in all three, both differ different ways -> CONFLICT; keep draft,
        record for surfacing
    """
    conflicts: list[dict] = []
    out: list[dict] = []
    all_ids = set(prior) | set(draft) | set(current)
    for uid in all_ids:
        p = prior.get(uid)
        d = draft.get(uid)
        c = current.get(uid)
        if c and not p and not d:
            out.append(c)                             # migration added; carry
        elif d and not p and not c:
            out.append(d)                             # user added; keep
        elif d and not c and p:
            if d == p:
                pass                                   # migration removed it; user hadn't touched -> drop
            else:
                # user edited an entry migration removed -> conflict; keep user copy
                out.append(d)
                conflicts.append({
                    "surface": surface, key_field: uid, "kind": "removed_by_migration_user_edited"})
        elif d and c and not p:
            # both added the same id? extremely unlikely (uuid). Keep draft.
            out.append(d)
            if d != c:
                conflicts.append({
                    "surface": surface, key_field: uid, "kind": "added_both"})
        elif d and c and p:
            d_changed = (d != p)
            c_changed = (c != p)
            if not d_changed and not c_changed:
                out.append(d)                          # quiet, no churn
            elif d_changed and not c_changed:
                out.append(d)                          # user edited only
            elif not d_changed and c_changed:
                out.append(c)                          # migration edited; silent merge
            else:                                       # both edited
                if d == c:
                    out.append(d)                      # coincident edit
                else:
                    out.append(d)                      # keep user; flag conflict
                    conflicts.append({
                        "surface": surface, key_field: uid, "kind": "both_edited"})
        # All other branches (e.g. in prior only, not in current, not in draft)
        # are silent drops — migration removed it, user already accepted.
    return out, conflicts


def _merge_3way_config(prior: dict, draft: dict, current: dict
                        ) -> tuple[dict, list]:
    """Flat-dict 3-way merge for config.yml. Same rules as the rid version,
    keyed by config field name."""
    conflicts: list[dict] = []
    out: dict = {}
    all_keys = set(prior) | set(draft) | set(current)
    for k in all_keys:
        p = prior.get(k)
        d = draft.get(k)
        c = current.get(k)
        in_prior = k in prior
        in_draft = k in draft
        in_current = k in current
        if in_current and not in_prior and not in_draft:
            out[k] = c
        elif in_draft and not in_prior and not in_current:
            out[k] = d
        elif in_draft and in_current and in_prior:
            d_changed = (d != p)
            c_changed = (c != p)
            if not d_changed and not c_changed:
                out[k] = d
            elif d_changed and not c_changed:
                out[k] = d
            elif not d_changed and c_changed:
                out[k] = c
            else:
                if d == c:
                    out[k] = d
                else:
                    out[k] = d
                    conflicts.append({
                        "surface": "config", "field": k, "kind": "both_edited"})
        elif in_draft and not in_current and in_prior:
            if d == p:
                pass
            else:
                out[k] = d
                conflicts.append({
                    "surface": "config", "field": k, "kind": "removed_by_migration_user_edited"})
        elif in_draft and in_current and not in_prior:
            out[k] = d
            if d != c:
                conflicts.append({
                    "surface": "config", "field": k, "kind": "added_both"})
    return out, conflicts


def _ensure_drafts_consistent() -> None:
    """alpha.20: per surface, ensure draft + baseline exist and are
    consistent with live.

    First boot on alpha.20 over alpha.19 live state:
      - drafts missing -> copy live to draft AND baseline; no merge needed
    Steady-state boot:
      - baseline == live -> no drift, leave draft as-is
    Migration drift (a step above mutated live):
      - 3-way merge prior=baseline, draft=draft, current=live; conflicts
        surface to .draft_reset_reasons; baseline ← live after merge

    Runs LAST in migrate.main() so it sees the final live shape after all
    other steps (rid backfill, system route seed, etc.) have settled.
    """
    all_conflicts: list[dict] = []

    for live_path, draft_path, baseline_path in SURFACE_TRIPLES:
        if not live_path.exists():
            # Edge: a surface whose live file is missing (shouldn't happen
            # after the bootstrap steps above). Skip; we have nothing to
            # seed draft from.
            continue
        live_bytes = live_path.read_bytes()

        if not draft_path.exists():
            # Fresh: copy live -> draft AND baseline.
            _atomic_write(draft_path, live_bytes.decode("utf-8"))
            _atomic_write(baseline_path, live_bytes.decode("utf-8"))
            print(
                f"migrate: alpha.20 — seeded draft for {live_path.name}",
                file=sys.stderr,
            )
            continue

        if not baseline_path.exists():
            # Draft exists but no baseline (mid-upgrade edge or someone
            # nuked the baseline). Assume current live IS the baseline;
            # no merge — would be guessing. Just snapshot baseline ← live.
            _atomic_write(baseline_path, live_bytes.decode("utf-8"))
            print(
                f"migrate: alpha.20 — reseeded baseline for {live_path.name} "
                "(was missing; no merge performed)",
                file=sys.stderr,
            )
            continue

        baseline_bytes = baseline_path.read_bytes()
        if baseline_bytes == live_bytes:
            continue  # no drift; quiet steady state

        # Drift detected — 3-way merge.
        prior_doc = yaml.safe_load(baseline_bytes.decode("utf-8")) or {}
        draft_doc = yaml.safe_load(draft_path.read_text()) or {}
        current_doc = yaml.safe_load(live_bytes.decode("utf-8")) or {}

        if live_path.name == "routes.yml":
            merged_list, conflicts = _merge_3way_rid_collection(
                _routes_by_rid(prior_doc),
                _routes_by_rid(draft_doc),
                _routes_by_rid(current_doc),
                surface="routes", key_field="rid",
            )
            merged_doc = {"routes": merged_list}
        elif live_path.name == "middlewares.yml":
            merged_list, conflicts = _merge_3way_rid_collection(
                _mws_by_mid(prior_doc),
                _mws_by_mid(draft_doc),
                _mws_by_mid(current_doc),
                surface="middlewares", key_field="mid",
            )
            merged_doc = {
                "version": current_doc.get("version", 1),
                "middlewares": merged_list,
            }
        else:  # config.yml
            merged_map, conflicts = _merge_3way_config(
                prior_doc, draft_doc, current_doc,
            )
            merged_doc = merged_map

        _atomic_write(draft_path, _dump(merged_doc))
        _atomic_write(baseline_path, live_bytes.decode("utf-8"))
        all_conflicts.extend(conflicts)
        print(
            f"migrate: alpha.20 — drift detected on {live_path.name}, "
            f"3-way merged ({len(conflicts)} conflict(s))",
            file=sys.stderr,
        )

    if all_conflicts:
        try:
            DRAFT_RESET_REASONS.write_text(json.dumps(all_conflicts, indent=2))
        except OSError as e:
            print(f"migrate: failed writing draft_reset_reasons: {e}",
                  file=sys.stderr)


def main() -> int:
    # alpha.20: complete any pending POST /api/apply BEFORE any other step
    # reads live. Exception-isolated; an unreadable journal leaves live
    # untouched and logs a warning.
    try:
        _recover_apply_journal()
    except Exception as e:
        print(f"migrate: apply-journal recovery failed: {e}", file=sys.stderr)

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
    try:
        _strip_route_tags()
    except Exception as e:
        print(f"migrate: route-tags strip failed: {e}", file=sys.stderr)
        bootstrap_step_rc = 1
    # alpha.20: rid + mid assignment must come AFTER any step that mutates
    # the live shape (ensure_ha_system_route seeds without rid;
    # ensure_builtin_middlewares seeds without mid; strip migrations rewrite
    # docs). Idempotent on steady state — one yaml load each, no write.
    try:
        _backfill_route_rid()
    except Exception as e:
        print(f"migrate: route rid backfill failed: {e}", file=sys.stderr)
        bootstrap_step_rc = 1
    try:
        _backfill_middleware_mid()
    except Exception as e:
        print(f"migrate: middleware mid backfill failed: {e}", file=sys.stderr)
        bootstrap_step_rc = 1
    # alpha.20: draft/baseline consistency runs LAST so it sees the final
    # live shape after all rid/mid/structural backfills.
    try:
        _ensure_drafts_consistent()
    except Exception as e:
        print(f"migrate: drafts consistency failed: {e}", file=sys.stderr)
        bootstrap_step_rc = 1

    return bootstrap_rc or bootstrap_step_rc


if __name__ == "__main__":
    sys.exit(main())
