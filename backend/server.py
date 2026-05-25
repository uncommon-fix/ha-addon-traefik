#!/usr/bin/env python3
"""Traefik add-on backend: aiohttp app serving the UI + /api/routes + dashboard proxy.

Architecture:
- Bound on 0.0.0.0:8080 (the add-on's ingress_port). HA ingress proxies all
  requests through; X-Ingress-Path tells us the base URL for the SPA.
- Reads /data/options.json (read-only) and /data/routes.yml (read-write).
- Save flow: PUT /api/routes -> validate -> atomic write routes.yml ->
  subprocess render.py (10s wait_for timeout) -> Traefik file provider
  hot-reloads. Serialised by an app-scoped asyncio.Lock so concurrent
  writers can't interleave.
- Reverse-proxies /dashboard/* and /traefik-api/* to Traefik's internal
  api.dashboard endpoint at 127.0.0.1:8090. App-scoped ClientSession via
  cleanup_ctx; hop-by-hop headers stripped per RFC 7230 §6.1.
"""
from __future__ import annotations

import asyncio
import html as _html
import ipaddress
import json
import os
import re
import shutil
import sys
from pathlib import Path

import aiohttp
import bcrypt
import yaml
from aiohttp import web

OPTIONS = Path("/data/options.json")
ROUTES_YML = Path("/data/routes.yml")
CONFIG_YML = Path("/data/config.yml")
MIDDLEWARES_YML = Path("/data/middlewares.yml")
WEB_ROOT = Path("/usr/share/traefik-web")
RENDER_PY = "/usr/local/bin/render.py"
TRAEFIK_URL = "http://127.0.0.1:8090"
RENDER_TIMEOUT = 10.0
# "Restart required" is content-based: cont-init writes .content_hash (the
# deployed integration content) and the integration writes .loaded_content_hash
# (what it actually loaded) — both under /config. Pending == they differ (or
# .loaded missing = deployed but not yet loaded). Self-clears on ANY restart
# because the integration rewrites .loaded_content_hash when it reloads, so the
# banner no longer depends on our button being the one that triggered it.
INTEGRATION_DIR = Path("/homeassistant/custom_components/traefik")
CONTENT_HASH_FILE = INTEGRATION_DIR / ".content_hash"
LOADED_HASH_FILE = INTEGRATION_DIR / ".loaded_content_hash"
# alpha.6: trusted_proxies quick-fix. Behind HA ingress + Traefik, HA Core
# returns 400 on forwarded HTTPS requests unless configuration.yaml trusts the
# supervisor proxy network. 172.30.32.0/23 is the supervisor mask (superset of
# the add-on's 172.30.33.0/24); use_x_forwarded_for must be set alongside it.
HA_CONFIGURATION_YAML = Path("/homeassistant/configuration.yaml")
TRUSTED_PROXY_CIDR = "172.30.32.0/23"
TRUSTED_PROXIES_SNIPPET = (
    "http:\n"
    "  use_x_forwarded_for: true\n"
    "  trusted_proxies:\n"
    "    - 172.30.32.0/23\n"
)
# Supervisor REST: SUPERVISOR_TOKEN is injected by HA when hassio_api: true
# is set in config.yaml. POST /addons/self/restart restarts our own
# container. Used after Setup save so the user doesn't have to click
# Restart in HA's UI for cont-init to re-export the CF_DNS_API_TOKEN.
SUPERVISOR_URL = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Core config schema. provider is the only enum today; future providers add
# their own credential fields here. cloudflare_token is the per-provider
# credential for the cloudflare provider; other providers will have other
# field names. acme_email + domain are provider-agnostic.
ALLOWED_PROVIDERS = {"cloudflare"}
ALLOWED_LOG_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR", "FATAL"}
# Traefik entryPoint names: lowercase letters/digits, '-', '_'. Reserved 'traefik'
# is excluded because we use it internally for the dashboard entryPoint.
ENTRYPOINT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
RESERVED_ENTRYPOINT_NAMES = {"traefik"}

# Phase F (0.7.0): ha_hostname is REMOVED from CONFIG_REQUIRED — it's now
# vestigial; the HA system route owns the subdomain. Wizard's 4-step payload
# would fail validation here if it stayed required. The field remains in
# CONFIG_TYPES (accepted-if-present) for back-compat with /data/config.yml
# files written pre-0.7.0.
CONFIG_REQUIRED = {"provider", "cloudflare_token", "acme_email",
                   "domain", "entrypoint_http",
                   "entrypoint_https", "log_level"}
CONFIG_TYPES = {
    "provider": str,
    "cloudflare_token": str,
    "acme_email": str,
    "domain": str,
    "ha_hostname": str,
    "entrypoint_http": str,
    "entrypoint_https": str,
    "log_level": str,
    # alpha.9: global HTTP->HTTPS redirect. When true, render.py adds an
    # entrypoint-level redirection (all :80 -> :443) and the per-route
    # redirect-to-https mechanism is superseded.
    "force_ssl": bool,
}

# Supervisor's documented ingress URL shape. Reject anything else to defuse a
# (theoretical) X-Ingress-Path XSS if the supervisor is ever compromised.
INGRESS_RE = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]{20,128}/?$")

# RFC 7230 §6.1 hop-by-hop; also strip Content-Length so aiohttp recomputes
# the framing instead of double-setting it alongside Transfer-Encoding.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}
# Frame-control headers we strip from the dashboard proxy response: we
# DELIBERATELY iframe Traefik's dashboard inside our SPA, but Traefik sets
# X-Frame-Options (and may set CSP frame-ancestors) on its responses that
# would make the browser refuse to render the iframe. Our SPA already sets
# its own CSP frame-ancestors='self' on serve_index, so the outer iframe
# (HA frontend -> our SPA) is locked down; stripping these only loosens the
# inner iframe (our SPA -> Traefik dashboard) which is what we want.
PROXY_DROP_HEADERS = HOP_BY_HOP | {"x-frame-options", "content-security-policy"}
# Whitelist of request headers forwarded upstream to Traefik dashboard.
# Strips Cookie / Authorization / X-Hass-* / X-Ingress-Path which Traefik
# doesn't understand and shouldn't see in its access log.
PROXY_REQ_HEADERS = {
    "accept", "accept-encoding", "accept-language", "user-agent",
    "if-none-match", "if-modified-since", "cache-control", "range",
}

ALLOWED_KINDS = {"home_assistant", "external"}
ALLOWED_SCHEMES = {"http", "https"}
REQUIRED = {"hostname", "backend_kind", "scheme", "tls", "enabled"}
# Phase F (0.7.0): closed set of system-route kinds. User PUTs cannot create
# or modify routes carrying these values (except for the hostname field).
ALLOWED_SYSTEM_KINDS = {"ha_self"}
ROUTE_TYPES = {
    "hostname": str,
    "backend_kind": str,
    "backend_host": (str, type(None)),
    "backend_port": (int, type(None)),
    "scheme": str,
    "tls": bool,
    "enabled": bool,
    "middlewares": list,
    # Phase E: per-route healthCheck.path override. Optional; renderer
    # defaults to "/" when absent or empty.
    "health_path": (str, type(None)),
    # Phase F: marks routes seeded/managed by the add-on (currently only
    # "ha_self"). Absent on user routes. User PUTs that try to set or change
    # this field on existing system routes are rejected; only the hostname
    # can be edited on a system row.
    "system": (str, type(None)),
}

# Phase F (0.7.0): middleware definitions surface.
# alpha.7: skipTlsVerify is a synthetic type — it carries no Traefik middleware;
# the renderer translates a route's skipTlsVerify middleware into a service-level
# serversTransport (insecureSkipVerify) so a route can front a self-signed HTTPS
# backend. It has no config.
ALLOWED_MIDDLEWARE_TYPES = {
    "basicAuth", "ipAllowList", "redirectScheme", "headers", "skipTlsVerify",
}
# alpha.7: add-on-managed built-ins. Server is the source of truth for
# "system-ness" (never trust a client flag): these names map to their canonical
# type, cannot be deleted or retyped via the UI/API, and are reconciled on every
# start by migrate.py. Their config is still user-editable where the type allows.
SYSTEM_MIDDLEWARE_NAMES = {
    "redirect-to-https": "redirectScheme",
    "skip-tls-verify": "skipTlsVerify",
}
MIDDLEWARE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
# Reserved as a UX choice (not a Traefik correctness requirement -- bare names
# get @file qualified internally so they can't actually collide with @internal).
RESERVED_MIDDLEWARE_NAMES = {"chain", "noop", "api", "dashboard"}
# basicAuth username regex -- rejects `:` (which would corrupt the
# user:hash htpasswd line that Traefik splits on first `:`), whitespace,
# control chars.
BASICAUTH_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
BCRYPT_ROUNDS = 12


# ---------- validation ----------
def _validate_routes(routes):
    # INVARIANT: pure on input. Does not mutate or normalise route dicts so
    # that _enforce_system_route_protection can compare on-disk vs incoming
    # routes by value. If a future contributor adds in-place normalisation,
    # the system-route protection silently breaks.
    if not isinstance(routes, list):
        raise web.HTTPBadRequest(text="payload.routes: must be list")
    # Track system kinds across the list (at-most-one per kind).
    seen_systems: dict[str, int] = {}
    for i, r in enumerate(routes):
        if not isinstance(r, dict):
            raise web.HTTPBadRequest(text=f"route[{i}]: must be object")
        unknown = set(r) - set(ROUTE_TYPES)
        if unknown:
            raise web.HTTPBadRequest(
                text=f"route[{i}]: unknown fields {sorted(unknown)}"
            )
        missing = REQUIRED - set(r)
        if missing:
            raise web.HTTPBadRequest(
                text=f"route[{i}]: missing required {sorted(missing)}"
            )
        for k, types in ROUTE_TYPES.items():
            if k in r and not isinstance(r[k], types):
                raise web.HTTPBadRequest(text=f"route[{i}].{k}: wrong type")
        if r["backend_kind"] not in ALLOWED_KINDS:
            raise web.HTTPBadRequest(text=f"route[{i}].backend_kind invalid")
        if r["scheme"] not in ALLOWED_SCHEMES:
            raise web.HTTPBadRequest(text=f"route[{i}].scheme invalid")
        if not r["hostname"].strip():
            raise web.HTTPBadRequest(text=f"route[{i}].hostname empty")
        if r["backend_kind"] == "external":
            host = r.get("backend_host")
            port = r.get("backend_port")
            if not host or not isinstance(port, int):
                raise web.HTTPBadRequest(
                    text=f"route[{i}]: external needs backend_host + backend_port"
                )
            if not (1 <= port <= 65535):
                raise web.HTTPBadRequest(
                    text=f"route[{i}].backend_port out of range"
                )
        mws = r.get("middlewares") or []
        if not all(isinstance(m, str) for m in mws):
            raise web.HTTPBadRequest(
                text=f"route[{i}].middlewares: not a list of strings"
            )
        # Phase E: health_path is optional. Belt+braces type check (the
        # ROUTE_TYPES loop above also catches non-str/non-None) for a
        # friendlier error message; semantic check enforces leading "/".
        hp = r.get("health_path")
        if hp is not None and not isinstance(hp, str):
            raise web.HTTPBadRequest(
                text=f"route[{i}].health_path: must be string or null"
            )
        if isinstance(hp, str) and hp and not hp.startswith("/"):
            raise web.HTTPBadRequest(
                text=f"route[{i}].health_path: must start with '/'"
            )
        # Phase F: system kind must be in the closed set, and at most one
        # route per kind.
        sys_kind = r.get("system")
        if sys_kind is not None:
            if sys_kind not in ALLOWED_SYSTEM_KINDS:
                raise web.HTTPBadRequest(
                    text=f"route[{i}].system: unknown kind {sys_kind!r} "
                         f"(allowed: {sorted(ALLOWED_SYSTEM_KINDS)})"
                )
            if sys_kind in seen_systems:
                raise web.HTTPBadRequest(
                    text=f"route[{i}].system={sys_kind!r}: only one "
                         "system route per kind allowed"
                )
            seen_systems[sys_kind] = i
    return routes


def _enforce_system_route_protection(incoming: list, existing: list) -> None:
    """Phase F: system routes are seeded + maintained by the add-on. User
    PUTs can rename the hostname but cannot create new system routes,
    delete existing ones, or modify any other field.

    MUST be called INSIDE save_lock with `existing` freshly loaded so a
    concurrent PUT can't pass-then-race the comparison.
    """
    existing_systems = {r["system"]: r for r in existing if r.get("system")}
    incoming_systems = {r["system"]: r for r in incoming if r.get("system")}
    # Every existing system route must still be present.
    for kind in existing_systems:
        if kind not in incoming_systems:
            raise web.HTTPBadRequest(
                text=f"cannot delete system route (kind={kind!r}); "
                     "system routes are managed by the add-on"
            )
    # No new system routes via the UI; locked fields must match on-disk.
    LOCKED = {"system", "backend_kind", "backend_host", "backend_port",
              "scheme", "tls", "enabled", "middlewares", "health_path"}
    for kind, new in incoming_systems.items():
        if kind not in existing_systems:
            raise web.HTTPBadRequest(
                text=f"cannot create system route (kind={kind!r}); "
                     "system routes are seeded by the add-on"
            )
        old = existing_systems[kind]
        for field in LOCKED:
            if new.get(field) != old.get(field):
                raise web.HTTPBadRequest(
                    text=f"system route field {field!r} is locked; "
                         "only the hostname can be edited"
                )
        # Belt+braces: empty hostname on an enabled system route would make
        # HA unreachable through Traefik. UI also guards this client-side.
        if new.get("enabled") and not (new.get("hostname") or "").strip():
            raise web.HTTPBadRequest(
                text=f"system route (kind={kind!r}) has empty hostname; "
                     "either set a hostname or contact the add-on dev"
            )


def _cross_reference_middlewares(routes: list, defined_names: set) -> None:
    """A route's middlewares list must only reference middleware names defined
    in /data/middlewares.yml. 400 at save time instead of letting Traefik
    silently 404 the router."""
    for i, r in enumerate(routes):
        for name in (r.get("middlewares") or []):
            if name and name not in defined_names:
                raise web.HTTPBadRequest(
                    text=f"route[{i}].middlewares: {name!r} is not defined "
                         "(create the middleware first on the Middlewares tab)"
                )


# ---------- middleware validation (Phase F) ----------
def _bcrypt_sync(plaintext: str) -> str:
    return bcrypt.hashpw(
        plaintext.encode("utf-8"),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
    ).decode("ascii")


async def _hash_password(plaintext: str) -> str:
    # bcrypt is CPU-bound; offload to the executor so the event loop stays
    # responsive -- rounds=12 is ~300-800ms on a Pi 3B+ and would block aiohttp.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _bcrypt_sync, plaintext)


def _existing_basicauth_users_by_name(existing_defs: list, mw_name: str) -> dict:
    """Look up existing basicAuth users for a given middleware name. Returns
    {username: password_hash} for the matching middleware (empty if absent
    or wrong type). Used to preserve hashes when the UI sends a blank
    password (which means 'keep the current one')."""
    for mw in existing_defs:
        if mw.get("name") == mw_name and mw.get("type") == "basicAuth":
            return {
                u["username"]: u.get("password_hash", "")
                for u in (mw.get("config", {}).get("users") or [])
                if u.get("username")
            }
    return {}


async def _validate_basicAuth(
    cfg: dict, existing_users_by_name: dict[str, str], mw_idx: int
) -> dict:
    if not isinstance(cfg, dict):
        raise web.HTTPBadRequest(text=f"middleware[{mw_idx}].config: must be object")
    users = cfg.get("users") or []
    if not isinstance(users, list) or not users:
        raise web.HTTPBadRequest(
            text=f"middleware[{mw_idx}].config.users: must be non-empty list"
        )
    out_users = []
    seen_names: set = set()
    for j, u in enumerate(users):
        if not isinstance(u, dict):
            raise web.HTTPBadRequest(
                text=f"middleware[{mw_idx}].config.users[{j}]: must be object"
            )
        username = (u.get("username") or "").strip()
        if not BASICAUTH_USERNAME_RE.match(username):
            raise web.HTTPBadRequest(
                text=f"middleware[{mw_idx}].config.users[{j}].username: must match "
                     f"{BASICAUTH_USERNAME_RE.pattern} (no ':', whitespace, or special chars)"
            )
        if username in seen_names:
            raise web.HTTPBadRequest(
                text=f"middleware[{mw_idx}].config.users[{j}].username: "
                     f"duplicate {username!r} in this middleware"
            )
        seen_names.add(username)
        password = u.get("password") or ""
        orig = u.get("_orig_username")
        if not password:
            # Blank password = keep existing hash. Look up by ORIGINAL name.
            if orig and orig in existing_users_by_name:
                if orig != username:
                    # rename + blank password is too easy a footgun; force a
                    # password re-entry on rename.
                    raise web.HTTPBadRequest(
                        text=f"middleware[{mw_idx}].config.users[{j}]: "
                             f"set a new password when renaming user "
                             f"{orig!r} -> {username!r}"
                    )
                hash_ = existing_users_by_name[orig]
            elif not orig and username in existing_users_by_name:
                # No _orig_username sent but a user with this name exists;
                # treat as identity-keep (handles UI quirks).
                hash_ = existing_users_by_name[username]
            else:
                raise web.HTTPBadRequest(
                    text=f"middleware[{mw_idx}].config.users[{j}]: "
                         f"password required for new user {username!r}"
                )
        else:
            hash_ = await _hash_password(password)
        out_users.append({"username": username, "password_hash": hash_})
    return {"users": out_users}


def _validate_ipAllowList(cfg: dict, mw_idx: int) -> dict:
    if not isinstance(cfg, dict):
        raise web.HTTPBadRequest(text=f"middleware[{mw_idx}].config: must be object")
    ranges = cfg.get("sourceRange") or []
    if not isinstance(ranges, list) or not ranges:
        raise web.HTTPBadRequest(
            text=f"middleware[{mw_idx}].config.sourceRange: must be non-empty list "
                 "(empty would block everything)"
        )
    normalised: list[str] = []
    for j, s in enumerate(ranges):
        if not isinstance(s, str) or not s.strip():
            raise web.HTTPBadRequest(
                text=f"middleware[{mw_idx}].config.sourceRange[{j}]: must be string"
            )
        try:
            # strict=False so 192.168.1.1/24 normalises to 192.168.1.0/24.
            net = ipaddress.ip_network(s.strip(), strict=False)
        except ValueError as e:
            raise web.HTTPBadRequest(
                text=f"middleware[{mw_idx}].config.sourceRange[{j}]={s!r}: {e}"
            )
        normalised.append(str(net))
    return {"sourceRange": normalised}


def _validate_redirectScheme(cfg: dict, mw_idx: int) -> dict:
    if not isinstance(cfg, dict):
        raise web.HTTPBadRequest(text=f"middleware[{mw_idx}].config: must be object")
    scheme = (cfg.get("scheme") or "").strip().lower()
    if scheme not in {"http", "https"}:
        raise web.HTTPBadRequest(
            text=f"middleware[{mw_idx}].config.scheme: must be 'http' or 'https'"
        )
    permanent = cfg.get("permanent", True)
    if not isinstance(permanent, bool):
        raise web.HTTPBadRequest(
            text=f"middleware[{mw_idx}].config.permanent: must be bool"
        )
    return {"scheme": scheme, "permanent": permanent}


def _validate_headers(cfg: dict, mw_idx: int) -> dict:
    if not isinstance(cfg, dict):
        raise web.HTTPBadRequest(text=f"middleware[{mw_idx}].config: must be object")
    out: dict = {}
    for key in ("customRequestHeaders", "customResponseHeaders"):
        d = cfg.get(key) or {}
        if not isinstance(d, dict):
            raise web.HTTPBadRequest(
                text=f"middleware[{mw_idx}].config.{key}: must be object"
            )
        filtered: dict = {}
        for k, v in d.items():
            if not isinstance(k, str):
                raise web.HTTPBadRequest(
                    text=f"middleware[{mw_idx}].config.{key}: keys must be strings"
                )
            k = k.strip()
            if not k:
                continue  # drop blank in-progress rows
            if not isinstance(v, str):
                raise web.HTTPBadRequest(
                    text=f"middleware[{mw_idx}].config.{key}.{k}: value must be string"
                )
            filtered[k] = v
        out[key] = filtered
    return out


async def _validate_middlewares(body, existing_defs: list) -> list:
    if not isinstance(body, list):
        raise web.HTTPBadRequest(text="payload.middlewares: must be list")
    seen_names: set = set()
    out: list = []
    for i, mw in enumerate(body):
        if not isinstance(mw, dict):
            raise web.HTTPBadRequest(text=f"middleware[{i}]: must be object")
        name = (mw.get("name") or "").strip()
        if not MIDDLEWARE_NAME_RE.match(name):
            raise web.HTTPBadRequest(
                text=f"middleware[{i}].name: must match {MIDDLEWARE_NAME_RE.pattern}"
            )
        if name in RESERVED_MIDDLEWARE_NAMES:
            raise web.HTTPBadRequest(
                text=f"middleware[{i}].name: {name!r} is reserved"
            )
        if name in seen_names:
            raise web.HTTPBadRequest(
                text=f"middleware[{i}].name: duplicate {name!r}"
            )
        seen_names.add(name)
        typ = mw.get("type")
        # System built-ins: the server owns their type — a client can't retype
        # them (e.g. turn redirect-to-https into a basicAuth). Force canonical.
        if name in SYSTEM_MIDDLEWARE_NAMES:
            typ = SYSTEM_MIDDLEWARE_NAMES[name]
        if typ not in ALLOWED_MIDDLEWARE_TYPES:
            raise web.HTTPBadRequest(
                text=f"middleware[{i}].type: must be one of {sorted(ALLOWED_MIDDLEWARE_TYPES)}"
            )
        # skipTlsVerify is a built-in-only synthetic type (renders to a service
        # serversTransport, not a Traefik middleware). Don't let users mint new
        # ones under arbitrary names.
        if typ == "skipTlsVerify" and name not in SYSTEM_MIDDLEWARE_NAMES:
            raise web.HTTPBadRequest(
                text=f"middleware[{i}].type: skipTlsVerify is reserved for the "
                     "built-in 'skip-tls-verify' middleware"
            )
        cfg = mw.get("config")
        if typ == "basicAuth":
            existing_users = _existing_basicauth_users_by_name(existing_defs, name)
            cfg_out = await _validate_basicAuth(cfg, existing_users, i)
        elif typ == "ipAllowList":
            cfg_out = _validate_ipAllowList(cfg, i)
        elif typ == "redirectScheme":
            cfg_out = _validate_redirectScheme(cfg, i)
        elif typ == "headers":
            cfg_out = _validate_headers(cfg, i)
        elif typ == "skipTlsVerify":
            cfg_out = {}  # no configuration
        else:
            raise web.HTTPInternalServerError(text="unreachable")
        out.append({"name": name, "type": typ, "config": cfg_out})
    return out


def _redact_middlewares(defs: list) -> list:
    """Strip basicAuth password_hash on every GET path. Returns a deep-copy
    so callers can mutate the result without polluting the on-disk shape."""
    out: list = []
    for mw in defs:
        copy = {
            "name": mw.get("name"),
            "type": mw.get("type"),
            # alpha.7: add-on-managed built-in (UI grays it out: name+type locked,
            # not removable). Derived server-side; never read from the stored def.
            "system": mw.get("name") in SYSTEM_MIDDLEWARE_NAMES,
        }
        cfg = mw.get("config") or {}
        if mw.get("type") == "basicAuth":
            copy["config"] = {
                "users": [
                    {
                        "username": u.get("username", ""),
                        "password_set": bool(u.get("password_hash")),
                    }
                    for u in (cfg.get("users") or [])
                ]
            }
        else:
            copy["config"] = cfg
        out.append(copy)
    return out


def _load_middlewares_yml() -> list:
    if not MIDDLEWARES_YML.exists():
        return []
    parsed = yaml.safe_load(MIDDLEWARES_YML.read_text()) or {}
    return parsed.get("middlewares") or []


def _system_default_config(name: str) -> dict:
    if name == "redirect-to-https":
        return {"scheme": "https", "permanent": True}
    return {}  # skip-tls-verify and any future no-config built-in


def _reinject_system_middlewares(defs: list, existing: list) -> list:
    """alpha.7: built-ins can't be deleted via the UI/API. After validation,
    re-add any system middleware that this PUT omitted — preserving its stored
    config (or seeding the default if it didn't exist yet) and its canonical
    type. Idempotent: names already present are left untouched."""
    present = {m.get("name") for m in defs}
    by_name = {m.get("name"): m for m in existing}
    for name, canon_type in SYSTEM_MIDDLEWARE_NAMES.items():
        if name in present:
            continue
        prior = by_name.get(name)
        cfg = (prior.get("config") if prior else None) or _system_default_config(name)
        defs.append({"name": name, "type": canon_type, "config": cfg})
    return defs


# ---------- helpers ----------
def _atomic_write_yml(path: Path, data: dict) -> None:
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(
        yaml.safe_dump(
            data,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=False,
            width=4096,
        )
    )
    os.replace(tmp, path)


def _load_config_yml() -> dict:
    # Merges /data/config.yml on top of /data/options.json so users mid-
    # migration see the same effective config as the renderer. config.yml
    # takes precedence; missing values fall back to supervisor options for
    # back-compat (Phase A-C installs).
    merged = {
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
    try:
        opts = json.loads(OPTIONS.read_text())
    except (OSError, json.JSONDecodeError):
        opts = {}
    if opts.get("acme_resolver"):
        merged["provider"] = opts["acme_resolver"]
    for field in ("cloudflare_token", "acme_email", "domain", "ha_hostname",
                  "entrypoint_http", "entrypoint_https", "log_level"):
        if opts.get(field):
            merged[field] = opts[field]
    if CONFIG_YML.exists():
        try:
            data = yaml.safe_load(CONFIG_YML.read_text()) or {}
            for field in merged:
                if field in data and data[field] is not None:
                    merged[field] = data[field]
        except yaml.YAMLError:
            pass
    return merged


def _validate_config(body):
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="payload: must be object")
    unknown = set(body) - set(CONFIG_TYPES)
    if unknown:
        raise web.HTTPBadRequest(text=f"unknown fields: {sorted(unknown)}")
    missing = CONFIG_REQUIRED - set(body)
    if missing:
        raise web.HTTPBadRequest(text=f"missing required: {sorted(missing)}")
    for k, t in CONFIG_TYPES.items():
        if k in body and not isinstance(body[k], t):
            raise web.HTTPBadRequest(text=f"{k}: wrong type (expected {t.__name__})")
    if body["provider"] not in ALLOWED_PROVIDERS:
        raise web.HTTPBadRequest(
            text=f"provider must be one of {sorted(ALLOWED_PROVIDERS)}"
        )
    email = body["acme_email"].strip()
    if email and "@" not in email:
        raise web.HTTPBadRequest(text="acme_email: invalid format")
    body["acme_email"] = email
    body["domain"] = body["domain"].strip().lower()
    body["cloudflare_token"] = body["cloudflare_token"].strip()
    body["provider"] = body["provider"].strip().lower()
    # ha_hostname is vestigial (the HA system route owns the subdomain) and the
    # setup wizard omits it; validate only when a client still sends it.
    if "ha_hostname" in body:
        ha = (body["ha_hostname"] or "").strip().lower()
        if ha and "." in ha:
            raise web.HTTPBadRequest(
                text="ha_hostname must be a bare subdomain (no dots); "
                     "e.g. 'hass' becomes 'hass.<domain>'"
            )
        body["ha_hostname"] = ha
    # entryPoint names: DNS-label-safe, non-empty, not reserved.
    for field in ("entrypoint_http", "entrypoint_https"):
        name = body[field].strip().lower()
        if not ENTRYPOINT_RE.match(name):
            raise web.HTTPBadRequest(
                text=f"{field}: must match {ENTRYPOINT_RE.pattern}"
            )
        if name in RESERVED_ENTRYPOINT_NAMES:
            raise web.HTTPBadRequest(
                text=f"{field}: {name!r} is reserved by Traefik"
            )
        body[field] = name
    if body["entrypoint_http"] == body["entrypoint_https"]:
        raise web.HTTPBadRequest(
            text="entrypoint_http and entrypoint_https must differ"
        )
    # log_level enum.
    lvl = body["log_level"].strip().upper()
    if lvl not in ALLOWED_LOG_LEVELS:
        raise web.HTTPBadRequest(
            text=f"log_level must be one of {sorted(ALLOWED_LOG_LEVELS)}"
        )
    body["log_level"] = lvl
    # force_ssl: optional bool (not in CONFIG_REQUIRED). Normalise if present.
    if "force_ssl" in body:
        body["force_ssl"] = bool(body["force_ssl"])
    return body


def _read_raw_config() -> dict:
    """Read /data/config.yml verbatim (PyYAML). Unlike _load_config_yml this
    does NOT drop unknown keys — needed so the dismiss marker and redirect_seeded
    survive a read-modify-write."""
    if not CONFIG_YML.exists():
        return {}
    try:
        data = yaml.safe_load(CONFIG_YML.read_text())
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


# ---------- integration banner (content-hash 3-state) ----------
def _integration_state() -> dict:
    """Drive the add-on integration banner from the two content-hash files:
    .content_hash (D, written by cont-init on deploy) and .loaded_content_hash
    (L, written by the integration's async_setup_entry — i.e. only once the user
    has ADDED it). Three states:
      - D absent (unreadable/empty)      -> ok (no banner), regardless of L
      - L absent                          -> available (deployed, never added)
      - L present, D != L                 -> update_pending (added, content changed)
      - L present, D == L                 -> ok
    `available` is suppressed once the user dismisses it FOR THE CURRENT D (a new
    deploy with new content re-surfaces it; adding the integration writes L and
    moots it)."""
    try:
        deployed = CONTENT_HASH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        deployed = ""
    if not deployed:
        return {"integration_pending_restart": False,
                "integration_available": False}
    try:
        loaded = LOADED_HASH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        loaded = ""
    if not loaded:
        dismissed_for = (
            _read_raw_config().get("integration_available_dismissed_for") or ""
        )
        return {"integration_pending_restart": False,
                "integration_available": dismissed_for != deployed}
    return {"integration_pending_restart": loaded != deployed,
            "integration_available": False}


# ---------- trusted_proxies quick-fix (alpha.6) ----------
class _FixBail(Exception):
    """Raised when configuration.yaml can't be safely auto-edited. The message
    is surfaced to the UI alongside the manual snippet."""


def _tolerant_yaml():
    """ruamel round-trip loader that tolerates unknown tags (!include /
    !include_dir_merge_named / !secret / !env_var) anywhere in the document.

    Round-trip mode preserves unknown tags natively (its construct_object
    fallback builds tagged CommentedMap/Seq/TaggedScalar proxies that dump
    faithfully). Do NOT register a None catch-all to construct_undefined — that
    method RAISES ('could not determine a constructor for the tag'), and
    registering it hijacks ruamel's native fallback. Raises ImportError if
    ruamel is unavailable (callers treat that as 'unsure')."""
    from ruamel.yaml import YAML

    y = YAML()  # round-trip by default; preserves unknown tags
    y.preserve_quotes = True
    y.width = 4096
    return y


def _tag_str(node) -> str | None:
    """The YAML tag string on a ruamel node, or None if untagged. Handles both
    the modern Tag-object API and the older string-tag API."""
    tag = getattr(node, "tag", None)
    if tag is None:
        return None
    val = getattr(tag, "value", None)
    if isinstance(val, str):
        return val
    return tag if isinstance(tag, str) else None


def _is_plain_mapping(node) -> bool:
    """A real, untagged mapping (so `http: !include x.yaml` / `http: !foo {}`
    are excluded and left for the user to edit by hand)."""
    return isinstance(node, dict) and _tag_str(node) is None


def _trusted_proxy_covered(tp) -> bool:
    """True if TRUSTED_PROXY_CIDR is a subnet of any entry in the list."""
    if not isinstance(tp, list):
        return False
    target = ipaddress.ip_network(TRUSTED_PROXY_CIDR)
    for entry in tp:
        try:
            net = ipaddress.ip_network(str(entry).strip(), strict=False)
        except ValueError:
            continue
        if target.version == net.version and target.subnet_of(net):
            return True
    return False


def _trusted_proxies_pending() -> bool:
    """True when configuration.yaml lacks the trusted_proxies / use_x_forwarded_for
    config that lets HTTPS-through-Traefik work. CONSERVATIVE: any uncertainty
    (missing file, parse error incl. DuplicateKeyError, tag-valued http:) returns
    False so split-config users are never nagged."""
    try:
        text = HA_CONFIGURATION_YAML.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = _tolerant_yaml().load(text)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    http = data.get("http")
    if http is None:
        return True  # no http block -> the keys are missing
    if not _is_plain_mapping(http):
        return False  # http: !include / tagged -> don't touch
    xff_ok = http.get("use_x_forwarded_for") is True
    tp_ok = _trusted_proxy_covered(http.get("trusted_proxies"))
    return not (xff_ok and tp_ok)


def _do_fix_trusted_proxies() -> None:
    """Sync (run in executor). Idempotently add use_x_forwarded_for + the
    supervisor CIDR to configuration.yaml's http: block, preserving comments and
    custom tags. Backs up first; the original is only replaced at the very end
    (so any earlier failure leaves it untouched). Raises _FixBail on any state we
    must not auto-edit."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    try:
        real = HA_CONFIGURATION_YAML.resolve()
    except OSError as e:
        raise _FixBail(f"cannot resolve configuration.yaml: {e}")
    if not real.exists():
        raise _FixBail("configuration.yaml not found")
    try:
        text = real.read_text(encoding="utf-8")
    except OSError as e:
        raise _FixBail(f"cannot read configuration.yaml: {e}")
    try:
        yaml_rt = _tolerant_yaml()
        data = yaml_rt.load(text)
    except Exception as e:
        raise _FixBail(f"configuration.yaml could not be parsed: {e}")

    if data is None:
        data = CommentedMap()
    if not isinstance(data, dict):
        raise _FixBail("configuration.yaml top level is not a mapping")

    http = data.get("http")
    if http is None:
        http = CommentedMap()
        data["http"] = http
    elif not _is_plain_mapping(http):
        raise _FixBail("http: is a tag node (e.g. !include); edit it by hand")

    if http.get("use_x_forwarded_for") is not True:
        http["use_x_forwarded_for"] = True
    tp = http.get("trusted_proxies")
    if not _trusted_proxy_covered(tp):
        if not isinstance(tp, list):
            tp = CommentedSeq()
            http["trusted_proxies"] = tp
        if not any(str(x).strip() == TRUSTED_PROXY_CIDR for x in tp):
            tp.append(TRUSTED_PROXY_CIDR)

    tmp = real.with_name(real.name + ".traefik-addon.tmp")
    bak = real.with_name(real.name + ".traefik-addon.bak")
    try:
        shutil.copy2(real, bak)  # preserves mode + mtime
    except OSError as e:
        raise _FixBail(f"cannot write backup: {e}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            yaml_rt.dump(data, f)
        shutil.copystat(real, tmp)
        os.replace(tmp, real)  # atomic; same fs
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise _FixBail(f"failed to write configuration.yaml: {e}")


def _config_state(config: dict) -> dict:
    """Onboarding state surfaced to the UI. Token value never leaves; only
    presence is reported."""
    # Onboarding asks for the user-facing setup fields only. The Advanced
    # fields all have working defaults and don't gate routing. Phase F:
    # ha_hostname is no longer in CONFIG_REQUIRED, so it's not in the
    # subtraction either.
    onboarding_fields = CONFIG_REQUIRED - {
        "entrypoint_http", "entrypoint_https", "log_level",
    }
    missing = [f for f in onboarding_fields if not (config.get(f) or "").strip()]
    state = {
        "configured": not missing,
        "missing": missing,
        # Boolean so the UI knows whether to show "token set" placeholder
        # vs an empty input. We NEVER return the token itself.
        "cloudflare_token_present": bool(config.get("cloudflare_token", "").strip()),
        # alpha.6: drives the trusted_proxies quick-fix banner.
        "trusted_proxies_pending": _trusted_proxies_pending(),
    }
    # alpha.6: integration banner is a 3-state, content-hash-derived signal.
    # integration_pending_restart (= update_pending) self-clears on restart;
    # integration_available (deployed-but-never-added) is dismissible.
    state.update(_integration_state())
    return state


def _redact_config(config: dict) -> dict:
    """Strip secrets from a config dict before returning to the UI."""
    redacted = dict(config)
    redacted["cloudflare_token"] = ""  # client sends new value or empty (= keep existing)
    return redacted


def _load_routes_yml() -> list:
    if not ROUTES_YML.exists():
        return []
    parsed = yaml.safe_load(ROUTES_YML.read_text()) or {}
    return parsed.get("routes") or []


def _strip_headers(headers, banned):
    return {k: v for k, v in headers.items() if k.lower() not in banned}


# ---------- handlers ----------
async def serve_index(request):
    raw = request.headers.get("X-Ingress-Path", "")
    ingress_path = raw if INGRESS_RE.match(raw) else ""
    ingress_path = _html.escape(ingress_path.rstrip("/"), quote=True)
    html_text = (WEB_ROOT / "index.html").read_text().replace(
        "{{INGRESS_PATH}}", ingress_path
    )
    resp = web.Response(text=html_text, content_type="text/html")
    # Same-origin iframe lock (HA ingress is same-origin to our SPA).
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    # Don't let the browser cache index.html -- it embeds {{INGRESS_PATH}}
    # which can rotate between supervisor restarts, and we need every page
    # load to pick up new app.js / version-busted asset URLs.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


async def get_routes(request):
    config = _load_config_yml()
    return web.json_response(
        {"domain": config.get("domain", ""), "routes": _load_routes_yml()}
    )


async def get_config(request):
    config = _load_config_yml()
    payload = _redact_config(config)
    payload["state"] = _config_state(config)
    return web.json_response(payload)


async def put_config(request):
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text=f"invalid JSON: {e}")
    # Preserve-existing-token: empty cloudflare_token in PUT means "keep the
    # value already stored." UI sends "" on every PUT unless the user types a
    # new token (the input is treated as write-only).
    current = _load_config_yml()
    if not (body.get("cloudflare_token") or "").strip():
        body["cloudflare_token"] = current.get("cloudflare_token", "")
    validated = _validate_config(body)

    async with request.app["save_lock"]:
        _atomic_write_yml(CONFIG_YML, validated)
        proc = await asyncio.create_subprocess_exec(
            "python3", RENDER_PY,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(), timeout=RENDER_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise web.HTTPGatewayTimeout(
                text=f"render.py exceeded {RENDER_TIMEOUT}s"
            )

    if proc.returncode != 0:
        raise web.HTTPInternalServerError(text=err.decode(errors="replace"))
    state = _config_state(validated)
    return web.json_response({
        "saved": True,
        "state": state,
        # If the token changed, the env var the traefik service inherited is
        # stale until cont-init re-runs (next addon restart). Surface this
        # so the UI can show a "Restart add-on to apply" banner.
        "restart_required": True,
        "stderr": err.decode(errors="replace"),
    })


async def get_state(request):
    config = _load_config_yml()
    return web.json_response(_config_state(config))


async def post_restart(request):
    """Restart our own add-on container via supervisor REST.

    The supervisor kills + recreates the container; this handler may not get
    to return -- our own process dies mid-flight. The UI handles that by
    showing a "Restarting..." state and polling /api/status until the
    backend comes back.
    """
    if not SUPERVISOR_TOKEN:
        raise web.HTTPInternalServerError(
            text="SUPERVISOR_TOKEN env var missing -- check hassio_api: true "
                 "in config.yaml and that the addon was rebuilt."
        )
    try:
        async with request.app["client"].post(
            f"{SUPERVISOR_URL}/addons/self/restart",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=10.0),
        ) as r:
            text = await r.text()
            if r.status >= 400:
                raise web.HTTPBadGateway(
                    text=f"supervisor returned {r.status}: {text}"
                )
    except aiohttp.ClientError as e:
        # We may also race the actual container kill here; treat
        # ClientError as "the request was sent but we got cut off",
        # which is the success path.
        return web.json_response({"restarting": True, "note": str(e)})
    return web.json_response({"restarting": True})


async def post_restart_core(request):
    """Phase 4: restart HA Core via the supervisor's proxied Core REST API.
    Triggered from the addon UI banner after 99-deploy-integration.sh writes
    the marker file.

    Path: POST http://supervisor/core/api/services/homeassistant/restart with
    Bearer SUPERVISOR_TOKEN. The supervisor proxies the call to HA Core's
    /api/services/homeassistant/restart service endpoint; auth gated by
    `homeassistant_api: true` in config.yaml.

    Why NOT POST /core/restart: that is supervisor's OWN restart-Core
    endpoint and requires `hassio_role: homeassistant`. We stay at
    `hassio_role: default` (least-privilege) and use Core's own service call
    instead. Verified live in Phase 4 deploy (the supervisor endpoint 403'd).

    Three retries with backoff so transient supervisor-startup races don't
    surface to the user. Token is NEVER logged. Body on failure is sanitised
    to the upstream status + a short reason string (no response body echo).
    """
    if not SUPERVISOR_TOKEN:
        raise web.HTTPInternalServerError(
            text="SUPERVISOR_TOKEN env var missing -- check homeassistant_api: true "
                 "in config.yaml and that the addon was rebuilt."
        )
    def _log_success(why: str) -> None:
        # Nothing to clear: the banner is content-based and self-clears once the
        # integration reloads and rewrites .loaded_content_hash.
        sys.stderr.write(f"INFO supervisor /core/restart succeeded ({why})\n")

    backoffs = (1.0, 2.0, 4.0)
    last_status = None
    last_reason = "no response"
    for attempt, sleep_for in enumerate(backoffs, start=1):
        try:
            async with request.app["client"].post(
                f"{SUPERVISOR_URL}/core/api/services/homeassistant/restart",
                headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
                json={},
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as r:
                last_status = r.status
                if r.status < 400:
                    _log_success(f"http {r.status} on attempt {attempt}")
                    return web.json_response({"restarting": True})
                last_reason = f"http {r.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # Mirror post_restart's behaviour: a timeout or connection error
            # on the first attempt means "the request was sent but Core
            # restarted itself mid-response." That IS the success path.
            # On retries, treat the same way — if a previous attempt landed,
            # Core's now booting and connection-refused/timeout is expected.
            _log_success(f"{type(e).__name__} on attempt {attempt} "
                         f"(treating as success — Core restarted mid-request)")
            return web.json_response(
                {"restarting": True, "note": type(e).__name__}
            )
        sys.stderr.write(
            f"WARN supervisor /core/restart attempt {attempt}/{len(backoffs)} "
            f"failed: {last_reason}\n"
        )
        if attempt < len(backoffs):
            await asyncio.sleep(sleep_for)
    raise web.HTTPBadGateway(
        text=f"supervisor /core/restart failed after {len(backoffs)} attempts "
             f"(last: {last_reason}, status={last_status})"
    )


async def post_fix_trusted_proxies(request):
    """alpha.6: add use_x_forwarded_for + the supervisor CIDR to HA's
    configuration.yaml so HTTPS-through-Traefik stops returning 400. The edit is
    comment-preserving, custom-tag-safe, backed up and idempotent; on any
    condition we can't safely auto-edit, bail with the manual snippet."""
    loop = asyncio.get_running_loop()
    async with request.app["save_lock"]:
        try:
            await loop.run_in_executor(None, _do_fix_trusted_proxies)
        except _FixBail as e:
            raise web.HTTPUnprocessableEntity(
                text=f"{e}\n\nAdd this to configuration.yaml by hand, then "
                     f"restart Home Assistant:\n\n{TRUSTED_PROXIES_SNIPPET}"
            )
    return web.json_response({"fixed": True})


async def post_dismiss_integration(request):
    """alpha.6: dismiss the 'reachability integration available' banner for the
    CURRENTLY-deployed integration content. A future deploy with new content
    re-surfaces it (content-scoped). Reads/writes config.yml RAW so unrelated
    keys (redirect_seeded, etc.) are preserved."""
    async with request.app["save_lock"]:
        raw = _read_raw_config()
        try:
            deployed = CONTENT_HASH_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            deployed = ""
        raw["integration_available_dismissed_for"] = deployed
        _atomic_write_yml(CONFIG_YML, raw)
    return web.json_response({"dismissed": True})


async def put_routes(request):
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text=f"invalid JSON: {e}")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="payload: must be object")

    # Load + protect + write + render ALL inside save_lock. Loading existing
    # state outside the lock would race with a concurrent PUT and let one
    # writer clobber the other's system route after both pass protection
    # independently.
    async with request.app["save_lock"]:
        existing = _load_routes_yml()
        incoming = _validate_routes(body.get("routes", []))
        # Cross-reference against current middlewares.
        defined_mw = {m["name"] for m in _load_middlewares_yml()}
        _cross_reference_middlewares(incoming, defined_mw)
        # System routes are seeded + locked.
        _enforce_system_route_protection(incoming, existing)
        # Persist with system routes at the front so the renderer's file-order
        # iteration matches.
        incoming = sorted(incoming, key=lambda r: 0 if r.get("system") else 1)
        _atomic_write_yml(ROUTES_YML, {"routes": incoming})
        proc = await asyncio.create_subprocess_exec(
            "python3", RENDER_PY,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(), timeout=RENDER_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise web.HTTPGatewayTimeout(
                text=f"render.py exceeded {RENDER_TIMEOUT}s"
            )

    if proc.returncode != 0:
        # DEBUG-FRIENDLY: returns raw stderr to the (authenticated admin) UI.
        raise web.HTTPInternalServerError(text=err.decode(errors="replace"))
    return web.json_response(
        {"saved": len(incoming), "stderr": err.decode(errors="replace")}
    )


async def get_middlewares(request):
    defs = _load_middlewares_yml()
    return web.json_response(
        {"version": 1, "middlewares": _redact_middlewares(defs)}
    )


async def put_middlewares(request):
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text=f"invalid JSON: {e}")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="payload: must be object")

    # Same locking discipline as put_routes.
    async with request.app["save_lock"]:
        existing = _load_middlewares_yml()
        defs = await _validate_middlewares(body.get("middlewares", []), existing)
        defs = _reinject_system_middlewares(defs, existing)
        _atomic_write_yml(MIDDLEWARES_YML, {"version": 1, "middlewares": defs})
        proc = await asyncio.create_subprocess_exec(
            "python3", RENDER_PY,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(), timeout=RENDER_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise web.HTTPGatewayTimeout(
                text=f"render.py exceeded {RENDER_TIMEOUT}s"
            )

    if proc.returncode != 0:
        raise web.HTTPInternalServerError(text=err.decode(errors="replace"))
    return web.json_response({
        "saved": len(defs),
        "stderr": err.decode(errors="replace"),
    })


async def get_status(request):
    """Reverse-proxy Traefik's /api/overview with friendlier 503-on-down."""
    try:
        async with request.app["client"].get(
            f"{TRAEFIK_URL}/api/overview",
            timeout=aiohttp.ClientTimeout(total=2.0),
        ) as r:
            return web.json_response(await r.json(), status=r.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return web.json_response(
            {"error": str(e), "traefik_up": False}, status=503
        )


# Injected at the top of the dashboard's <head> so the bundled SPA's
# fetch('/api/overview') / XHR calls get prefixed with our ingress-served
# /dashboard/api/ path. Without this, the absolute-path fetches hit HA Core
# (the iframe's effective origin) and 404. We read the prefix from
# window.location.pathname at runtime so it's session-token-agnostic.
DASHBOARD_REWRITE_SCRIPT = b"""<script>
(function () {
  var m = window.location.pathname.match(/^(.*)\\/dashboard\\//);
  if (!m) return;
  var BASE = m[1] + '/dashboard';  // e.g. /api/hassio_ingress/<token>/dashboard
  function rewrite(url) {
    if (typeof url === 'string' && url.charAt(0) === '/' && url.indexOf('/api/') === 0) {
      return BASE + url;
    }
    return url;
  }
  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    if (typeof input === 'string') return origFetch.call(this, rewrite(input), init);
    if (input && input.url) return origFetch.call(this, new Request(rewrite(input.url), input), init);
    return origFetch.call(this, input, init);
  };
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    arguments[1] = rewrite(url);
    return origOpen.apply(this, arguments);
  };
})();
</script>"""


async def _proxy(request, upstream_url, *, inject_dashboard_rewrite=False):
    fwd = {
        k: v for k, v in request.headers.items()
        if k.lower() in PROXY_REQ_HEADERS
    }
    try:
        async with request.app["client"].request(
            request.method,
            upstream_url,
            data=request.content,
            headers=fwd,
            allow_redirects=False,
        ) as upstream:
            out_headers = _strip_headers(upstream.headers, PROXY_DROP_HEADERS)
            content_type = upstream.headers.get("Content-Type", "")
            is_html = inject_dashboard_rewrite and "text/html" in content_type.lower()

            if is_html:
                # Buffer the full body so we can inject the rewrite script
                # at the top of <head>. The dashboard HTML is ~kilobytes; fine
                # to buffer in memory. Drop Content-Length so aiohttp recomputes
                # after the injection (already in PROXY_DROP_HEADERS but
                # restate for clarity).
                body = await upstream.read()
                idx = body.lower().find(b"<head>")
                if idx != -1:
                    insert_at = idx + len(b"<head>")
                    body = body[:insert_at] + DASHBOARD_REWRITE_SCRIPT + body[insert_at:]
                return web.Response(
                    status=upstream.status, headers=out_headers, body=body
                )

            resp = web.StreamResponse(
                status=upstream.status, headers=out_headers
            )
            await resp.prepare(request)
            async for chunk in upstream.content.iter_chunked(65536):
                await resp.write(chunk)
            return resp
    except aiohttp.ClientConnectorError:
        raise web.HTTPServiceUnavailable(text="Traefik dashboard unreachable")


async def proxy_dashboard_api(request):
    # Rewritten by DASHBOARD_REWRITE_SCRIPT: /api/foo -> /dashboard/api/foo.
    # Strip the /dashboard prefix and forward to Traefik's actual /api/*.
    tail = request.match_info["tail"]
    qs = f"?{request.query_string}" if request.query_string else ""
    return await _proxy(request, f"{TRAEFIK_URL}/api/{tail}{qs}")


async def proxy_dashboard(request):
    tail = request.match_info["tail"]
    qs = f"?{request.query_string}" if request.query_string else ""
    # Only inject the rewrite script on the dashboard's HTML index, not on
    # JS/CSS/font assets (would corrupt them and is wasted work anyway).
    inject = tail in ("", "index.html")
    return await _proxy(
        request,
        f"{TRAEFIK_URL}/dashboard/{tail}{qs}",
        inject_dashboard_rewrite=inject,
    )


async def proxy_traefik_api(request):
    tail = request.match_info["tail"]
    qs = f"?{request.query_string}" if request.query_string else ""
    return await _proxy(request, f"{TRAEFIK_URL}/api/{tail}{qs}")


# ---------- lifecycle ----------
async def client_session_ctx(app):
    app["client"] = aiohttp.ClientSession()
    app["save_lock"] = asyncio.Lock()
    yield
    await app["client"].close()


def make_app():
    app = web.Application()
    app.cleanup_ctx.append(client_session_ctx)
    app.router.add_get("/", serve_index)
    app.router.add_static("/static", str(WEB_ROOT / "static"))
    app.router.add_get("/api/routes", get_routes)
    app.router.add_put("/api/routes", put_routes)
    app.router.add_get("/api/middlewares", get_middlewares)
    app.router.add_put("/api/middlewares", put_middlewares)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/status", get_status)
    app.router.add_post("/api/restart", post_restart)
    app.router.add_post("/api/restart-core", post_restart_core)
    app.router.add_post("/api/fix-trusted-proxies", post_fix_trusted_proxies)
    app.router.add_post("/api/dismiss-integration", post_dismiss_integration)
    # NOTE: /dashboard/api/* must register BEFORE /dashboard/* so it wins
    # the route match (aiohttp resolves in registration order). It catches
    # the dashboard SPA's monkey-patched API fetches and forwards to
    # Traefik's actual /api/*.
    app.router.add_route("*", "/dashboard/api/{tail:.*}", proxy_dashboard_api)
    app.router.add_route("*", "/dashboard/{tail:.*}", proxy_dashboard)
    app.router.add_route("*", "/traefik-api/{tail:.*}", proxy_traefik_api)
    return app


if __name__ == "__main__":
    # shutdown_timeout=2.0 matches s6-overlay's default S6_SERVICES_GRACETIME
    # (3000ms) so in-flight requests drain before s6 sends SIGKILL.
    web.run_app(
        make_app(),
        host="0.0.0.0",
        port=8080,
        shutdown_timeout=2.0,
    )
