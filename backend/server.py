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
import ipaddress
import json
import os
import re
import shutil
import sys
import time
import traceback
import uuid
from pathlib import Path

import aiohttp
import bcrypt
import yaml
from aiohttp import web

# The shared add-on kit, vendored at backend/addonkit/ by tools/sync-shared.ps1.
# Resolvable as a plain name for the same reason `providers` below is: this file
# is executed as a script (python3 /usr/local/bin/backend/server.py), which puts
# its own directory on sys.path. Never edit backend/addonkit/ -- it is a copy.
from addonkit.gate import Gate, gate_middleware
from addonkit.ingress import ingress_path as kit_ingress_path
from addonkit.ingress import render as kit_render
from addonkit.ingress import view_headers as kit_view_headers

OPTIONS = Path("/data/options.json")
ROUTES_YML = Path("/data/routes.yml")
CONFIG_YML = Path("/data/config.yml")
MIDDLEWARES_YML = Path("/data/middlewares.yml")
# alpha.20: draft/live split. PUT handlers write the *.draft.yml files; only
# POST /api/apply copies them to live + runs render. Renderer reads live only.
ROUTES_DRAFT_YML = Path("/data/routes.draft.yml")
CONFIG_DRAFT_YML = Path("/data/config.draft.yml")
MIDDLEWARES_DRAFT_YML = Path("/data/middlewares.draft.yml")
# alpha.20: baseline files = live bytes at the moment draft was last
# initialized / last Apply'd. Used for 3-way merge on live drift; bumped
# by post_apply after a successful render.
ROUTES_BASELINE_YML = Path("/data/.routes.baseline.yml")
CONFIG_BASELINE_YML = Path("/data/.config.baseline.yml")
MIDDLEWARES_BASELINE_YML = Path("/data/.middlewares.baseline.yml")
# alpha.20: apply journal written before live rename — completed/cleaned by
# migrate._recover_apply_journal on the next boot if Apply crashed mid-way.
APPLY_JOURNAL = Path("/data/.apply_journal.yml")
DRAFT_RESET_REASONS = Path("/data/.draft_reset_reasons.json")
# (live, draft, baseline) per surface — iterated by apply / discard / pending.
SURFACE_TRIPLES = (
    (ROUTES_YML, ROUTES_DRAFT_YML, ROUTES_BASELINE_YML),
    (MIDDLEWARES_YML, MIDDLEWARES_DRAFT_YML, MIDDLEWARES_BASELINE_YML),
    (CONFIG_YML, CONFIG_DRAFT_YML, CONFIG_BASELINE_YML),
)
WEB_ROOT = Path("/usr/share/traefik-web")
RENDER_PY = "/usr/local/bin/render.py"
TRAEFIK_URL = "http://127.0.0.1:8090"
RENDER_TIMEOUT = 10.0
# alpha.14: addon version, exported by the Dockerfile (ENV ADDON_VERSION=${BUILD_VERSION}).
# Used as a query-string cache-buster on the app.js <script src> so users get the
# new bundle on every release without hard-refresh. Fail-fast at import: a
# misconfigured build is louder than a literal "{{APP_VERSION}}" rendered to HTML.
ADDON_VERSION = os.environ["ADDON_VERSION"]
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
# Providers and the credentials each needs live in backend/providers.py --
# one table, shared with render.py and cont-init. "local" is in the enum but
# is not an ACME provider: Traefik serves its own self-signed certificate and
# every browser warns. It exists so someone with no DNS account is not
# blocked at the wizard on a credential they may never have.
# Absolute, not relative: this file is executed as a script
# (python3 /usr/local/bin/backend/server.py), so it has no parent package and
# `from .providers import` would raise at startup. Running a script puts its
# own directory on sys.path, which is what makes the plain name resolve.
from providers import (  # noqa: E402
    ALL_CREDENTIAL_ENV,
    ALLOWED_PROVIDERS,
    PROVIDER_LOCAL,
    required_env,
    ui_catalog,
)
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
    # {ENV_VAR_NAME: value} for the selected provider. Values are write-only
    # over the API: GET never returns them, only which names are set.
    "provider_credentials": dict,
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

# The X-Ingress-Path whitelist now lives in addonkit.ingress (INGRESS_RE +
# ingress_path). The local copy this replaced ended `$`, which in Python also
# matches immediately before a trailing newline -- so a header value ending in
# "\n" passed the whitelist and was smuggled into the page. The kit ends `\Z`.

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
    # alpha.14: per-route "skip TLS verification on the backend" flag. Replaces
    # the alpha.7 model where this was a magic-string middleware attachment;
    # `skip-tls-verify` was never a real Traefik middleware (render translates
    # it into a service-level serversTransport), so it now lives where it
    # belongs — on the route. Optional; renderer defaults to False when absent.
    "skip_tls_verify": bool,
    # alpha.20: per-route stable identity. Server-generated uuid4 (assigned
    # by migrate._backfill_route_rid on boot if missing; assigned by put_routes
    # on a fresh route). Preserved through draft round-trips; the per-field
    # diff endpoint keys on this so hostname renames don't surface as
    # add+delete. Hidden from the user; UI never displays it.
    "rid": (str, type(None)),
    # Phase F: marks routes seeded/managed by the add-on (currently only
    # "ha_self"). Absent on user routes. User PUTs that try to set or change
    # this field on existing system routes are rejected; only the hostname
    # can be edited on a system row.
    "system": (str, type(None)),
}

# Phase F (0.7.0): middleware definitions surface.
ALLOWED_MIDDLEWARE_TYPES = {
    "basicAuth", "ipAllowList", "redirectScheme", "headers",
}
# alpha.7: add-on-managed built-ins. Server is the source of truth for
# "system-ness" (never trust a client flag): these names map to their canonical
# type, cannot be deleted or retyped via the UI/API, and are reconciled on every
# start by migrate.py. Their config is still user-editable where the type allows.
# alpha.14: skip-tls-verify removed (moved to route.skip_tls_verify bool).
SYSTEM_MIDDLEWARE_NAMES = {
    "redirect-to-https": "redirectScheme",
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

# ---------- application keys ----------
# web.AppKey rather than plain strings: aiohttp 3.9+ raises NotAppKeyWarning on
# every string-keyed app[...] and 4.0 removes them outright. A typed key also
# cannot silently collide with an entry some other component puts on the app,
# which a bare "client" very much can. Created in client_session_ctx below;
# CLIENT is the only one aiohttp itself owns the lifetime of.
CLIENT: web.AppKey[aiohttp.ClientSession] = web.AppKey("traefik.client")
# apply_lock: held for the whole of POST /api/apply (snapshot + stage + journal
# + rename + render + baseline update), and by the other handlers that mutate
# live so they cannot race Apply.
APPLY_LOCK: web.AppKey[asyncio.Lock] = web.AppKey("traefik.apply_lock")
# draft_write_lock: held during PUT-to-draft (cheap, no render) and briefly
# inside Apply for the snapshot+stage+rename phase.
DRAFT_WRITE_LOCK: web.AppKey[asyncio.Lock] = web.AppKey("traefik.draft_write_lock")
# The single-editor lock -- addonkit.gate.Gate. Constructed in make_app rather
# than in the cleanup_ctx because gate_middleware closes over the instance and
# the middleware list is built before the app starts.
GATE: web.AppKey[Gate] = web.AppKey("traefik.gate")


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

    MUST be called INSIDE the draft/apply lock with `existing` freshly loaded
    so a concurrent PUT can't pass-then-race the comparison. (Named save_lock
    before the alpha.20 split; that alias is gone -- see APPLY_LOCK above.)
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
    # alpha.14: skip_tls_verify added — toggling skip-TLS on the HA backend
    # would be both nonsensical (it's not an HTTPS backend) and a trust-boundary
    # issue if a client could mutate it.
    LOCKED = {"system", "backend_kind", "backend_host", "backend_port",
              "scheme", "tls", "enabled", "middlewares", "health_path",
              "skip_tls_verify"}
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
        else:
            raise web.HTTPInternalServerError(text="unreachable")
        # alpha.20: preserve `mid` (uuid4 per middleware). Middleware names
        # are user-editable, so identity tracking for the per-row diff lives
        # in `mid` not `name`. _validate_middlewares is constructive (drops
        # any unknown input key), so we MUST explicitly carry `mid` through.
        mid = mw.get("mid")
        out_dict = {"name": name, "type": typ, "config": cfg_out}
        if mid:
            out_dict["mid"] = mid
        out.append(out_dict)
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
        # alpha.20: per-row stable identity for diff/sort/rollback. Always
        # present after migrate._backfill_middleware_mid runs (first boot
        # of alpha.20+); fresh middlewares get a mid in put_middlewares.
        if mw.get("mid"):
            copy["mid"] = mw["mid"]
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


def _load_middlewares_from(path: Path) -> list:
    if not path.exists():
        return []
    try:
        parsed = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise web.HTTPInternalServerError(
            text=f"{path} has invalid YAML and the add-on refuses to "
                 f"overwrite it. Fix the file by hand before saving. ({e})"
        )
    return parsed.get("middlewares") or []


def _load_middlewares_yml() -> list:
    return _load_middlewares_from(MIDDLEWARES_YML)


def _load_middlewares_draft() -> list:
    if MIDDLEWARES_DRAFT_YML.exists():
        return _load_middlewares_from(MIDDLEWARES_DRAFT_YML)
    return _load_middlewares_yml()


def _system_default_config(name: str) -> dict:
    if name == "redirect-to-https":
        return {"scheme": "https", "permanent": True}
    return {}


def _reinject_system_middlewares(defs: list, existing: list) -> list:
    """alpha.7: built-ins can't be deleted via the UI/API. After validation,
    re-add any system middleware that this PUT omitted — preserving its stored
    config (or seeding the default if it didn't exist yet) and its canonical
    type. Idempotent: names already present are left untouched. alpha.20:
    preserves `mid` on the reinjected built-in so its identity survives the
    discard-all → re-PUT round-trip; without this, every save without an
    explicit built-in would generate a fresh mid and the diff would explode."""
    present = {m.get("name") for m in defs}
    by_name = {m.get("name"): m for m in existing}
    for name, canon_type in SYSTEM_MIDDLEWARE_NAMES.items():
        if name in present:
            continue
        prior = by_name.get(name)
        cfg = (prior.get("config") if prior else None) or _system_default_config(name)
        entry = {"name": name, "type": canon_type, "config": cfg}
        if prior and prior.get("mid"):
            entry["mid"] = prior["mid"]
        defs.append(entry)
    return defs


# alpha.20: extracted render+rollback helper. Used by post_apply (the only
# path that renders now); the per-PUT renders moved to drafts so the dev
# UX of "edit freely, see traefik update only on Apply" works.
async def _run_render_with_rollback(snapshots: dict[Path, bytes | None]
                                     ) -> tuple[bool, str]:
    """Spawns render.py; on timeout or non-zero exit, restores each path in
    `snapshots` to its captured bytes (or unlinks if the value is None) so
    cont-init's next-boot render starts from a known-good state. Returns
    (ok, stderr_text). stderr is redacted before returning."""
    proc = await asyncio.create_subprocess_exec(
        "python3", RENDER_PY,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=RENDER_TIMEOUT
        )
        err = _redact(err_bytes.decode(errors="replace"))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        for path, prior in snapshots.items():
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, prior)
        return False, f"render.py exceeded {RENDER_TIMEOUT}s"
    if proc.returncode != 0:
        for path, prior in snapshots.items():
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, prior)
        return False, err
    return True, err


# ---------- helpers ----------
# Token-shaped substring redactor. Cheap insurance: render.py / subprocess stderr
# returned to the UI gets matched against the Cloudflare-token shape and masked.
# Same character class + length window as _validate_config's token regex.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,256}")


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("<redacted>", text)


def _fsync_parent_dir(path: Path) -> None:
    # Persist the directory entry rename to disk. Without this, a power loss
    # between the os.replace and the writeback can leave the directory entry
    # pointing at the OLD inode while the data blocks are gone.
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    # Crash-safe: write to a sibling tmp, flush + fsync the data, atomic rename,
    # then fsync the parent dir. Mirrored by _atomic_write_yml; used directly by
    # the snapshot-restore path in put_* on render failure.
    tmp = path.parent / (path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_parent_dir(path)


def _atomic_write_yml(path: Path, data: dict) -> None:
    payload = yaml.safe_dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=False,
        width=4096,
    )
    _atomic_write_bytes(path, payload.encode("utf-8"))


def _load_config_from(path: Path) -> dict:
    """Merges <path> on top of /data/options.json so the caller sees the
    effective config (back-compat with Phase A-C installs that only had
    options.json). Path is either CONFIG_YML (live) or CONFIG_DRAFT_YML
    (alpha.20 draft view)."""
    merged = {
        "provider": "cloudflare",
        "cloudflare_token": "",
        # {ENV_NAME: value} for the selected provider. This default MUST be
        # here: the config.yml pass below only copies keys that are ALREADY in
        # `merged`, so without it every stored credential was silently dropped
        # on load -- credentials_present reported nothing set, no provider but
        # the legacy cloudflare one could ever count as configured, and the
        # next save wrote the map back out empty.
        "provider_credentials": {},
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
                  "provider_credentials",
                  "entrypoint_http", "entrypoint_https", "log_level"):
        if opts.get(field):
            merged[field] = opts[field]
    if path.exists():
        # Corrupt config used to silently fall back to defaults — and the
        # next save would then overwrite the file with the validated payload,
        # erasing any stored unknown keys (redirect_seeded,
        # integration_available_dismissed_for). Refuse instead: surface a
        # 500 with the parse error and let the user fix the file by hand.
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise web.HTTPInternalServerError(
                text=f"{path} has invalid YAML and the add-on refuses to "
                     f"overwrite it. Fix the file by hand before saving. ({e})"
            )
        if not isinstance(data, dict):
            raise web.HTTPInternalServerError(
                text=f"{path} top level is not a YAML mapping; "
                     "expected a dict of fields."
            )
        for field in merged:
            if field in data and data[field] is not None:
                merged[field] = data[field]
    return merged


def _load_config_yml() -> dict:
    return _load_config_from(CONFIG_YML)


def _load_config_draft() -> dict:
    """alpha.20: draft view. Falls back to live if the draft hasn't been
    seeded yet."""
    if CONFIG_DRAFT_YML.exists():
        return _load_config_from(CONFIG_DRAFT_YML)
    return _load_config_yml()


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
    token = body["cloudflare_token"].strip()
    # Reject embedded whitespace / control chars. .strip() only trims the
    # edges, so a multi-line paste used to survive validation, get exported
    # via printf '%s' in cont-init, and silently break Cloudflare auth.
    # Same character class as _TOKEN_RE used for stderr redaction.
    if token and not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", token):
        raise web.HTTPBadRequest(
            text="cloudflare_token: must be 20-256 chars from "
                 "[A-Za-z0-9_-] only (no whitespace or newlines)."
        )
    body["cloudflare_token"] = token
    body["provider"] = body["provider"].strip().lower()

    # provider_credentials: the same character rules the cloudflare token has
    # always had. A credential with an embedded newline validates fine,
    # exports fine, and then fails at certificate-issue time, which is the
    # worst place to find out.
    #
    # Names are deliberately NOT restricted to the selected provider's set. A
    # credential for a provider you are not using right now is PARKED, not an
    # error: switching provider -- or to `local` -- must not destroy a token
    # you may switch back to, which is the promise useLocalProvider already
    # makes about the legacy cloudflare_token. cont-init exports only
    # required_env(provider) and deletes the rest, so a parked value never
    # reaches Traefik's environment. This also has to hold because post_apply
    # re-validates the stored draft: a rule that rejected parked names would
    # make Apply fail on a config this very endpoint wrote.
    # Names the add-on knows nothing about are still rejected -- that is a
    # client bug, not a parked secret.
    creds = body.get("provider_credentials") or {}
    unknown_creds = set(creds) - set(ALL_CREDENTIAL_ENV)
    if unknown_creds:
        raise web.HTTPBadRequest(
            text=f"provider_credentials: unknown credential name(s) "
                 f"{sorted(unknown_creds)}"
        )
    cleaned = {}
    for name, value in creds.items():
        if not isinstance(value, str):
            raise web.HTTPBadRequest(
                text=f"provider_credentials.{name}: must be a string"
            )
        value = value.strip()
        if not value:
            continue          # blank means "keep what is stored"
        if not re.fullmatch(r"[\x21-\x7e]{1,512}", value):
            raise web.HTTPBadRequest(
                text=f"provider_credentials.{name}: must be 1-512 printable "
                     "characters with no whitespace or newlines."
            )
        cleaned[name] = value
    body["provider_credentials"] = cleaned
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
    survive a read-modify-write. Refuses on corrupt YAML: silently swallowing
    used to let post_dismiss_integration replace the entire file with a single
    key when config.yml was unparseable."""
    if not CONFIG_YML.exists():
        return {}
    try:
        data = yaml.safe_load(CONFIG_YML.read_text())
    except yaml.YAMLError as e:
        raise web.HTTPInternalServerError(
            text=f"/data/config.yml has invalid YAML and the add-on refuses "
                 f"to overwrite it. Fix the file by hand before saving. ({e})"
        )
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
    # Refuse to overwrite an earlier backup. A previous Fix click left a
    # known-good copy; clobbering it on a re-run would lose the original.
    # The user can delete the .bak by hand if they really want a fresh one.
    if bak.exists():
        raise _FixBail(
            f"an earlier backup ({bak.name}) is already present next to "
            "configuration.yaml. Review or delete it before re-running so "
            "the original known-good copy isn't overwritten."
        )
    try:
        shutil.copy2(real, bak)  # preserves mode + mtime
    except OSError as e:
        raise _FixBail(f"cannot write backup: {e}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            yaml_rt.dump(data, f)
            # Crash-safe: persist data + the directory rename to disk so a
            # power loss between close and writeback can't leave a zero-byte
            # configuration.yaml in place.
            f.flush()
            os.fsync(f.fileno())
        shutil.copystat(real, tmp)
        os.replace(tmp, real)  # atomic; same fs
        _fsync_parent_dir(real)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise _FixBail(f"failed to write configuration.yaml: {e}")


def _config_state(config: dict) -> dict:
    """Onboarding state surfaced to the UI. Credential values never leave;
    only presence is reported.

    `configured` is the single flag the frontend routes on: false means the
    onboarding page is the whole UI, true means the dashboard mounts. What it
    takes to be configured is PROVIDER-DEPENDENT, and cannot be the fixed
    field list it used to be:

      - the Advanced fields (entry points, log level) all have working
        defaults and never gate routing, so they are not asked about;
      - `local` issues no certificates at all, so it needs neither an ACME
        contact nor any credential -- demanding one would leave a user who
        deliberately chose self-signed permanently stuck in onboarding;
      - every other provider needs ITS OWN credentials. The old list
        hardcoded `cloudflare_token`, which no provider added by the provider
        table ever writes, so all eleven of them were unreachable.
    """
    creds = _credentials_present(config)
    provider = (config.get("provider") or "").strip().lower()
    missing: list[str] = []
    if provider not in ALLOWED_PROVIDERS:
        missing.append("provider")
    elif provider != PROVIDER_LOCAL:
        if not (config.get("acme_email") or "").strip():
            missing.append("acme_email")
        missing.extend(env for env in required_env(provider) if not creds.get(env))
    if not (config.get("domain") or "").strip():
        missing.append("domain")
    state = {
        "configured": not missing,
        "missing": missing,
        # Booleans so the UI can show a "already set" placeholder instead of
        # an empty box. We NEVER return a credential value.
        "cloudflare_token_present": bool((config.get("cloudflare_token") or "").strip()),
        "credentials_present": creds,
        # alpha.6: drives the trusted_proxies quick-fix banner.
        "trusted_proxies_pending": _trusted_proxies_pending(),
    }
    # alpha.6: integration banner is a 3-state, content-hash-derived signal.
    # integration_pending_restart (= update_pending) self-clears on restart;
    # integration_available (deployed-but-never-added) is dismissible.
    state.update(_integration_state())
    return state


def _credentials_present(config: dict) -> dict:
    """{ENV_NAME: bool} for every credential this add-on knows about.

    Includes the legacy `cloudflare_token` spelling under its env name, so an
    install predating the provider table still shows its token as set.
    """
    stored = dict(config.get("provider_credentials") or {})
    legacy = (config.get("cloudflare_token") or "").strip()
    if legacy and not (stored.get("CF_DNS_API_TOKEN") or "").strip():
        stored["CF_DNS_API_TOKEN"] = legacy
    return {env: bool((stored.get(env) or "").strip())
            for env in sorted(ALL_CREDENTIAL_ENV)}


async def get_providers(request: web.Request) -> web.Response:
    """The provider catalogue the wizard builds its fields from.

    Field DEFINITIONS only -- never values. Served from the same table
    render.py and cont-init use, so the form can never offer a field the
    exporter would ignore.
    """
    return web.json_response({"providers": ui_catalog()})


def _redact_config(config: dict) -> dict:
    """Strip secrets from a config dict before returning to the UI."""
    redacted = dict(config)
    redacted["cloudflare_token"] = ""  # client sends new value or empty (= keep existing)
    # Credential VALUES are write-only over the API (see CONFIG_TYPES). The UI
    # learns which names are set from state.credentials_present and nothing
    # more -- dropping the key here is what makes that true now that
    # _load_config_from actually reads the map back off disk.
    redacted.pop("provider_credentials", None)
    return redacted


def _load_routes_from(path: Path) -> list:
    """Generic YAML-doc loader for a routes file (live OR draft). Raises
    HTTPInternalServerError on a corrupt file rather than returning an empty
    list so we don't silently overwrite the user's data."""
    if not path.exists():
        return []
    try:
        parsed = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise web.HTTPInternalServerError(
            text=f"{path} has invalid YAML and the add-on refuses "
                 f"to overwrite it. Fix the file by hand before saving. ({e})"
        )
    return parsed.get("routes") or []


def _load_routes_yml() -> list:
    return _load_routes_from(ROUTES_YML)


def _load_routes_draft() -> list:
    """alpha.20: draft view. Falls back to live if the draft hasn't been
    seeded yet (very first boot before _ensure_drafts_consistent ran)."""
    if ROUTES_DRAFT_YML.exists():
        return _load_routes_from(ROUTES_DRAFT_YML)
    return _load_routes_yml()


def _strip_headers(headers, banned):
    return {k: v for k, v in headers.items() if k.lower() not in banned}


# ---------- handlers ----------
async def serve_index(request):
    # kit_render substitutes in ONE pass, so a value can never be re-scanned
    # for another token; the chained .replace() calls this replaced expanded
    # whatever the previous substitution had just inserted.
    html_text = kit_render(
        (WEB_ROOT / "index.html").read_text(),
        {},                       # no fragment files: this add-on has one template
        INGRESS_PATH=kit_ingress_path(request),
        APP_VERSION=ADDON_VERSION,
    )
    resp = web.Response(text=html_text, content_type="text/html")
    # Same-origin iframe lock (HA ingress is same-origin to our SPA), plus
    # no-store: index.html embeds {{INGRESS_PATH}}, which can rotate between
    # supervisor restarts, and every load must pick up version-busted assets.
    resp.headers.update(kit_view_headers())
    return resp


async def get_routes(request):
    """alpha.20: returns the draft view by default; ?live=1 returns the
    currently-rendered live snapshot for the per-row diff in the UI. The
    config domain comes from the draft too so a draft hostname-rewrite
    previews correctly against the user's pending domain change."""
    live = request.query.get("live") == "1"
    config = _load_config_yml() if live else _load_config_draft()
    routes = _load_routes_yml() if live else _load_routes_draft()
    return web.json_response(
        {"domain": config.get("domain", ""), "routes": routes}
    )


async def get_config(request):
    """alpha.20: returns draft by default; ?live=1 returns live."""
    live = request.query.get("live") == "1"
    config = _load_config_yml() if live else _load_config_draft()
    payload = _redact_config(config)
    payload["state"] = _config_state(config)
    return web.json_response(payload)


async def put_config(request):
    """alpha.20: writes draft config (CONFIG_DRAFT_YML). Does NOT render —
    only post_apply renders. The "restart required" flag is gone from this
    response because draft-only writes can't trigger a restart-need; the
    Apply flow will compute it when it commits live."""
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text=f"invalid JSON: {e}")
    # _validate_config checks this too, but the preserve-existing merges below
    # index into `body` first and would 500 on a list.
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="payload: must be object")
    # Preserve-existing-token: empty cloudflare_token in PUT means "keep the
    # value already stored." Read from CURRENT DRAFT (not live) so a user who
    # has been editing the token sees the same write-only behaviour against
    # their pending value, not against the stale live one.
    current = _load_config_draft()
    if not (body.get("cloudflare_token") or "").strip():
        body["cloudflare_token"] = current.get("cloudflare_token", "")
    # Same preserve-existing rule for the per-provider credentials, and what
    # makes the wizard's "••••• (set; leave blank to keep)" placeholder true:
    # _validate_config drops blank values, so without this merge every save
    # that didn't retype the secret erased it. Stored names the payload
    # doesn't mention are carried through untouched -- see the parked-
    # credential note in _validate_config.
    stored_creds = {
        name: value
        for name, value in (current.get("provider_credentials") or {}).items()
        if isinstance(value, str) and value.strip()
    }
    incoming_creds = body.get("provider_credentials")
    if incoming_creds is not None and not isinstance(incoming_creds, dict):
        raise web.HTTPBadRequest(
            text="provider_credentials: wrong type (expected dict)"
        )
    merged_creds = dict(stored_creds)
    for name, value in (incoming_creds or {}).items():
        if isinstance(value, str) and value.strip():
            merged_creds[name] = value
    body["provider_credentials"] = merged_creds
    validated = _validate_config(body)

    async with request.app[DRAFT_WRITE_LOCK]:
        _atomic_write_yml(CONFIG_DRAFT_YML, validated)

    state = _config_state(validated)
    return web.json_response({"saved": True, "state": state})


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
        async with request.app[CLIENT].post(
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
            async with request.app[CLIENT].post(
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
    # alpha.20: apply_lock — this handler writes outside /data so it doesn't
    # need draft_write_lock, but it's an exclusive long-running action so it
    # serializes against Apply.
    async with request.app[APPLY_LOCK]:
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
    # alpha.20: apply_lock — this writes config.yml LIVE directly (server-
    # managed state field, not a user-editable draft surface), so it
    # serializes against Apply rather than draft writes.
    async with request.app[APPLY_LOCK]:
        raw = _read_raw_config()
        try:
            deployed = CONTENT_HASH_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            deployed = ""
        raw["integration_available_dismissed_for"] = deployed
        _atomic_write_yml(CONFIG_YML, raw)
    return web.json_response({"dismissed": True})


async def put_routes(request):
    """alpha.20: writes the DRAFT routes file (ROUTES_DRAFT_YML). The
    system-route protection check still runs against LIVE — locked fields
    on the HA self-route are immutable regardless of which file we're
    writing. Cross-reference against the DRAFT middlewares so a user can
    add a middleware in draft and reference it from a new route in the same
    edit session. No render — post_apply is the only path that renders."""
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text=f"invalid JSON: {e}")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="payload: must be object")

    async with request.app[DRAFT_WRITE_LOCK]:
        # Protection check is always against LIVE — locked fields are
        # immutable, draft or not.
        existing_live = _load_routes_yml()
        incoming = _validate_routes(body.get("routes", []))
        # Cross-reference against DRAFT middlewares so an in-progress add
        # is reachable.
        defined_mw = {m["name"] for m in _load_middlewares_draft()}
        _cross_reference_middlewares(incoming, defined_mw)
        _enforce_system_route_protection(incoming, existing_live)
        # Auto-assign rid to any route lacking one (fresh "Add route"
        # entries from the UI). Match-by-system-kind first so the seeded
        # HA self-route keeps its existing rid even if the frontend forgot
        # to round-trip it.
        live_by_rid = {r.get("rid"): r for r in existing_live if r.get("rid")}
        live_systems = {r.get("system"): r for r in existing_live if r.get("system")}
        for r in incoming:
            if r.get("rid"):
                continue
            # No rid on incoming. Try to match an existing live row so we
            # don't regenerate identity by accident (alpha.15 lesson —
            # hand-rolled serializers drift). Match priority: system kind.
            sys_kind = r.get("system")
            if sys_kind and sys_kind in live_systems:
                r["rid"] = live_systems[sys_kind].get("rid") or str(uuid.uuid4())
            else:
                r["rid"] = str(uuid.uuid4())
        # Persist with system routes at the front so the renderer's file-order
        # iteration matches.
        incoming = sorted(incoming, key=lambda r: 0 if r.get("system") else 1)
        _atomic_write_yml(ROUTES_DRAFT_YML, {"routes": incoming})

    return web.json_response({"saved": len(incoming)})


# ------------------- alpha.23: cross-addon internal API ------------------
# Sibling addons (e.g. davinci-resolve) can POST here to scaffold a route
# in this addon's DRAFT — the user then reviews + clicks Apply in this
# addon's UI to actually publish. Bypasses session_gate (callers are
# addons, not user tabs) + version_gate (callers don't know our app
# version). Authentication: requires a non-empty Bearer token in the
# Authorization header. The supervisor's bridge network is internal-only,
# so any container that reaches us is by construction another addon on
# the same HA install — adequate for homelab MVP. Stronger auth (verify
# the token via supervisor /info) is a future improvement.

INTERNAL_API_MAX_FIELD_LEN = 256


async def post_internal_routes(request):
    """alpha.23: scaffold a new route into the DRAFT from a sibling addon
    request. Body (all fields optional except `name`):
        {
            "name": "davinci-resolve",       # used as the route hostname
            "backend_kind": "external",      # default "external"
            "backend_host": "local_davinci-resolve",   # addon hostname on bridge
            "backend_port": 5432,
            "scheme": "http",                # default "http"
            "tls": false,                    # default false
            "source": "davinci-resolve"      # informational; logged
        }
    Returns: {"rid": "<uuid>", "name": <name>}.

    The created route is APPENDED to the existing draft routes list. The
    user reviews + Applies in the UI. We DO NOT auto-Apply — sibling
    addons mutating live config without an explicit user gesture would
    be a surprise.

    Validation: route shape goes through the same `_validate_routes` as
    PUT /api/routes; duplicate hostname check rejects re-scaffolds.
    """
    # 1. Auth: non-empty Authorization header (homelab trust).
    auth = request.headers.get("Authorization", "")
    if not auth.strip():
        return web.json_response(
            {"error": "Authorization header required (bearer token)."},
            status=401,
        )

    # 2. Parse body.
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text=f"invalid JSON: {e}")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="payload: must be object")

    # 3. Extract + clamp lengths so a misbehaving caller can't shove huge
    #    strings into routes.yml. Field-by-field rather than a blanket
    #    iterate so we stay explicit about what we accept.
    def _str_field(key: str, default: str = "") -> str:
        v = body.get(key, default)
        if not isinstance(v, str):
            raise web.HTTPBadRequest(text=f"{key}: must be string")
        return v[:INTERNAL_API_MAX_FIELD_LEN]

    name = _str_field("name").strip()
    if not name:
        raise web.HTTPBadRequest(text="name: required (used as the route hostname)")

    backend_kind = _str_field("backend_kind", "external").strip() or "external"
    backend_host = _str_field("backend_host").strip() or None
    backend_port = body.get("backend_port")
    if backend_port is not None and not isinstance(backend_port, int):
        raise web.HTTPBadRequest(text="backend_port: must be int")
    scheme = _str_field("scheme", "http").strip() or "http"
    tls = bool(body.get("tls", False))
    source = _str_field("source").strip() or "internal"

    # 4. Build the candidate route dict in the same shape put_routes accepts.
    candidate = {
        "hostname": name,
        "backend_kind": backend_kind,
        "backend_host": backend_host,
        "backend_port": backend_port,
        "scheme": scheme,
        "tls": tls,
        "enabled": True,
        "middlewares": [],
    }

    # 5. Load draft, dedupe, append, validate, write — all inside
    #    draft_write_lock to serialise against other PUT/internal calls.
    async with request.app[DRAFT_WRITE_LOCK]:
        existing_draft = _load_routes_draft()
        existing_hostnames = {(r.get("hostname") or "").strip().lower()
                              for r in existing_draft}
        if name.lower() in existing_hostnames:
            return web.json_response(
                {"error": f"a route named {name!r} already exists in the draft",
                 "code": "ROUTE_EXISTS"},
                status=409,
            )
        # Combine into the full list and run the existing validator —
        # gives us the same coverage as PUT /api/routes (kind, scheme,
        # port range, hostname non-empty, middleware list of strings).
        combined = list(existing_draft) + [candidate]
        validated = _validate_routes(combined)
        # System-route protection (HA self-route locked fields) compares
        # to LIVE; cross-reference middlewares against DRAFT same as PUT.
        existing_live = _load_routes_yml()
        defined_mw = {m["name"] for m in _load_middlewares_draft()}
        _cross_reference_middlewares(validated, defined_mw)
        _enforce_system_route_protection(validated, existing_live)
        # Assign rid to the new entry (it's the only one without one).
        live_by_rid = {r.get("rid"): r for r in existing_live if r.get("rid")}
        for r in validated:
            if r.get("rid"):
                continue
            r["rid"] = str(uuid.uuid4())
        validated = sorted(validated, key=lambda r: 0 if r.get("system") else 1)
        _atomic_write_yml(ROUTES_DRAFT_YML, {"routes": validated})

        # Locate the newly-added rid (by hostname match — name is unique
        # by the dedupe check above).
        new_route = next(
            (r for r in validated if (r.get("hostname") or "").lower() == name.lower()),
            None,
        )
        new_rid = new_route.get("rid") if new_route else None

    sys.stderr.write(
        f"INFO internal route scaffolded: name={name!r} source={source!r} "
        f"rid={new_rid!r}\n"
    )
    return web.json_response({
        "rid": new_rid,
        "name": name,
        "message": "Route scaffolded in draft. Open the Traefik addon UI and click Apply to publish.",
    })


async def get_middlewares(request):
    """alpha.20: returns draft by default; ?live=1 returns live."""
    live = request.query.get("live") == "1"
    defs = _load_middlewares_yml() if live else _load_middlewares_draft()
    return web.json_response(
        {"version": 1, "middlewares": _redact_middlewares(defs)}
    )


async def put_middlewares(request):
    """alpha.20: writes the DRAFT middlewares file. The existing-basicAuth-
    users preservation looks at DRAFT (not live) so an in-progress edit
    doesn't lose its own pending user list when the user types in the input.
    Reinjection of system built-ins also reads DRAFT for the same reason.
    No render — Apply does it."""
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text=f"invalid JSON: {e}")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="payload: must be object")

    async with request.app[DRAFT_WRITE_LOCK]:
        existing = _load_middlewares_draft()
        defs = await _validate_middlewares(body.get("middlewares", []), existing)
        defs = _reinject_system_middlewares(defs, existing)
        # Auto-assign mid to any middleware lacking one (fresh "Add
        # middleware" from the UI). Match-by-name first against live to
        # preserve identity for renames that the frontend missed (alpha.15
        # lesson). Note: matching against LIVE here (not draft) so a name
        # round-trip from a downgrade scenario picks up the live mid.
        live_defs = _load_middlewares_yml()
        live_by_name = {m.get("name"): m for m in live_defs if m.get("name")}
        for m in defs:
            if m.get("mid"):
                continue
            n = m.get("name")
            if n and n in live_by_name and live_by_name[n].get("mid"):
                m["mid"] = live_by_name[n]["mid"]
            else:
                m["mid"] = str(uuid.uuid4())
        _atomic_write_yml(
            MIDDLEWARES_DRAFT_YML, {"version": 1, "middlewares": defs}
        )

    return web.json_response({"saved": len(defs)})


def _effective_slug(raw_hostname: str, domain: str) -> str | None:
    """Mirror render.py's hostname→slug rule so route-health keys match the
    rendered Traefik service names. None for routes the renderer skips."""
    raw = (raw_hostname or "").strip().lower()
    if "*" in raw:
        return None  # wildcard routes are skipped by the renderer
    if domain:
        if raw in ("", "@"):
            host = domain
        elif "." in raw:
            return None  # invalid when a domain is set (renderer skips it)
        else:
            host = f"{raw}.{domain}"
    else:
        host = raw
    if not host:
        return None
    return host.replace(".", "-")


async def get_route_health(request):
    """Per-route backend reachability for the UI's status dots. Maps each route
    (keyed by its raw hostname) to "up" | "down" | "unknown" | "disabled" using
    Traefik's serverStatus (same logic as the reachability integration). The
    backend owns the slug computation so the UI doesn't replicate it."""
    config = _load_config_yml()
    domain = (config.get("domain") or "").strip().lower()
    routes = _load_routes_yml()
    try:
        async with request.app[CLIENT].get(
            f"{TRAEFIK_URL}/api/http/services",
            timeout=aiohttp.ClientTimeout(total=2.0),
        ) as r:
            services = await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"traefik_up": False, "health": {}}, status=200)

    svc_health: dict[str, str] = {}
    for svc in services if isinstance(services, list) else []:
        name = svc.get("name", "")
        if not name or name.endswith("@internal"):
            continue
        slug = name[:-5] if name.endswith("@file") else name
        server_status = svc.get("serverStatus") or {}
        if not server_status:
            svc_health[slug] = "unknown"  # healthCheck hasn't run yet
        else:
            up = svc.get("status") == "enabled" and all(
                v == "UP" for v in server_status.values()
            )
            svc_health[slug] = "up" if up else "down"

    health: dict[str, str] = {}
    for rt in routes:
        host = rt.get("hostname", "")
        if not rt.get("enabled", True):
            health[host] = "disabled"
            continue
        slug = _effective_slug(host, domain)
        health[host] = svc_health.get(slug, "unknown") if slug else "unknown"
    return web.json_response({"traefik_up": True, "health": health})


async def get_status(request):
    """Reverse-proxy Traefik's /api/overview with friendlier 503-on-down."""
    try:
        async with request.app[CLIENT].get(
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
        async with request.app[CLIENT].request(
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


# ---------- session (alpha.12) ----------
# Single-writer concurrent-edit guard. The implementation is now
# addonkit.gate.Gate, which was generalised FROM the SessionManager that used
# to live right here, so the model is unchanged: ONE active edit session,
# mutating endpoints require the caller's X-Session-Id to match, otherwise 423.
# A second tab is prompted to "Take over" (which invalidates the current
# session) or "View read-only" (no SID, all mutations forbidden).
#
# Heartbeat: any request carrying X-Session-Id refreshes last_seen if the SID
# matches. The frontend's existing /api/state poll (every 5s) keeps the session
# alive; no separate heartbeat endpoint needed. After SESSION_TTL of no
# heartbeat the session expires server-side and a new claim succeeds.
#
# Two things the kit does better, inherited here for free: its clock is
# time.monotonic, so an NTP step can no longer expire or extend a live session
# (the code this replaced used time.time), and a refused claim returns "" where
# the old one returned None -- either way a reader never learns the holder's
# SID, but the kit makes that a stated guarantee rather than an accident.
SESSION_TTL = 60.0

# (method, ROUTE PATTERN) of endpoints that REQUIRE X-Session-Id to match the
# current session. Read endpoints, the session/claim/takeover endpoints
# themselves, the SPA at /, the /static/ assets, the dashboard proxy paths and
# the /api/internal/* sibling bridge are all UNGATED -- multiple readers are
# fine; only writers need serialising.
#
# The kit matches the REGISTERED ROUTE PATTERN, not the concrete path: it keys
# on request.match_info.route.resource.canonical, so a dynamic endpoint would
# be listed as e.g. ("DELETE", "/api/routes/{rid}"). Every gated endpoint below
# is a static route, so pattern and path coincide today; the distinction starts
# to matter the moment one of them grows a path variable.
GATED_MUTATIONS: set[tuple[str, str]] = {
    ("PUT", "/api/config"),
    ("PUT", "/api/routes"),
    ("PUT", "/api/middlewares"),
    ("POST", "/api/fix-trusted-proxies"),
    ("POST", "/api/dismiss-integration"),
    ("POST", "/api/restart"),
    ("POST", "/api/restart-core"),
    # alpha.20: draft/live + Apply.
    ("POST", "/api/apply"),
    ("POST", "/api/discard"),
}

# Kept verbatim from the SessionManager era instead of taking the kit's default
# wording. app.js discards the 423 body and keys on the status alone, but the
# string still reaches the add-on log, and changing user-visible text is not
# what this migration is for.
LOCKED_MESSAGE = (
    "Session not current. Another tab or browser is editing; "
    "reload to claim a new session or take over."
)


async def post_session_claim(request):
    gate: Gate = request.app[GATE]
    # alpha.15: pass the requester's X-Session-Id through to claim() so a
    # caller already holding the active session gets a no-op refresh instead
    # of 409-ing itself. (The gate middleware also heartbeats on any matching
    # SID before we get here, so claim() then short-circuits to success.)
    incoming_sid = request.headers.get("X-Session-Id") or None
    sid, claimed = gate.claim(incoming_sid)
    if claimed:
        return web.json_response({"sid": sid})
    # 409 + current_age_s is the shape app.js's takeover prompt reads. The kit
    # ships its own claim handler that answers 200/{"claimed": false}; this
    # frontend predates it, so the ENDPOINT stays ours and only the Gate is
    # shared. Switching the status is a frontend change, not a kit adoption.
    return web.json_response({"current_age_s": gate.age_s() or 0.0}, status=409)


async def post_session_takeover(request):
    """Forcibly become the active session. UNGATED by design: a freshly
    opened second tab has no current SID and still needs to be able to take
    over. The UI surfaces this as an explicit 'Take over' button on the
    "another session active" modal."""
    gate: Gate = request.app[GATE]
    return web.json_response({"sid": gate.takeover()})


# ---------- lifecycle ----------
@web.middleware
async def json_error_mw(request, handler):
    """Wrap every error response as `{"error": "..."}` JSON. Without this:
    - HTTPException(text=...) returns `Content-Type: text/plain` and the UI
      shows the bare string in a font-mono red box.
    - Any unhandled Exception escapes to aiohttp's default handler, which
      returns a full Python traceback in the body — surfaced to the user.

    Proxy paths (/dashboard/*, /traefik-api/*) pass through on success; only
    error responses get wrapped.
    """
    try:
        return await handler(request)
    except web.HTTPException as ex:
        if ex.content_type == "application/json":
            raise
        body = {"error": ex.text or ex.reason or f"HTTP {ex.status}"}
        return web.json_response(body, status=ex.status)
    except Exception:
        # Send the traceback to the add-on log (visible in Settings → Add-ons
        # → Traefik → Log), NOT to the response body where the user would see
        # it. Surface a generic message.
        sys.stderr.write(traceback.format_exc())
        return web.json_response({"error": "internal error"}, status=500)


# ---------- alpha.20: draft/live diff + Apply + Discard ----------

def _routes_diff(draft: list, live: list) -> dict:
    """Compute the per-rid diff between draft and live routes.
    Returns {modified: [rid], added: [rid], deleted: [rid]}."""
    draft_by_rid = {r.get("rid"): r for r in draft if r.get("rid")}
    live_by_rid = {r.get("rid"): r for r in live if r.get("rid")}
    modified = []
    for rid, d in draft_by_rid.items():
        if rid in live_by_rid and d != live_by_rid[rid]:
            modified.append(rid)
    added = sorted(set(draft_by_rid) - set(live_by_rid))
    deleted = sorted(set(live_by_rid) - set(draft_by_rid))
    return {"modified": sorted(modified), "added": added, "deleted": deleted}


def _middlewares_diff(draft: list, live: list) -> dict:
    """Same shape as _routes_diff but keyed on mid."""
    draft_by_mid = {m.get("mid"): m for m in draft if m.get("mid")}
    live_by_mid = {m.get("mid"): m for m in live if m.get("mid")}
    modified = []
    for mid, d in draft_by_mid.items():
        if mid in live_by_mid and d != live_by_mid[mid]:
            modified.append(mid)
    added = sorted(set(draft_by_mid) - set(live_by_mid))
    deleted = sorted(set(live_by_mid) - set(draft_by_mid))
    return {"modified": sorted(modified), "added": added, "deleted": deleted}


def _config_diff(draft: dict, live: dict) -> dict:
    """Flat-dict diff. Returns {modified: [field_name]}."""
    keys = set(draft) | set(live)
    modified = [k for k in keys if draft.get(k) != live.get(k)]
    return {"modified": sorted(modified)}


def _pending_warnings(draft_routes: list, draft_config: dict) -> list[dict]:
    """Routes whose draft state would be silently skipped by render.py.
    Currently: force_ssl on + tls:false (render WARN-skips them per
    alpha.9). Surfaced in the Apply banner's details dropdown so the user
    knows BEFORE clicking Apply that the route won't actually serve."""
    warnings: list[dict] = []
    if draft_config.get("force_ssl"):
        for r in draft_routes:
            if not r.get("tls") and r.get("enabled", True) and not r.get("system"):
                warnings.append({
                    "kind": "force_ssl_skip",
                    "rid": r.get("rid"),
                    "hostname": r.get("hostname"),
                    "message": "Force SSL is on but this route has TLS off; "
                               "render will skip it.",
                })
    return warnings


async def get_pending(request):
    """alpha.20: per-surface diff between draft and live + the total change
    count + any preview warnings. Computed on demand from disk; cheap
    enough that the frontend can poll on a debounce-tick without worry."""
    draft_routes = _load_routes_draft()
    live_routes = _load_routes_yml()
    draft_mws = _load_middlewares_draft()
    live_mws = _load_middlewares_yml()
    draft_config = _load_config_draft()
    live_config = _load_config_yml()

    rd = _routes_diff(draft_routes, live_routes)
    md = _middlewares_diff(draft_mws, live_mws)
    cd = _config_diff(draft_config, live_config)
    total = (len(rd["modified"]) + len(rd["added"]) + len(rd["deleted"])
             + len(md["modified"]) + len(md["added"]) + len(md["deleted"])
             + len(cd["modified"]))
    payload = {
        "routes": rd,
        "middlewares": md,
        "config": cd,
        "warnings": _pending_warnings(draft_routes, draft_config),
        "total": total,
    }
    # Surface and clear the 3-way-merge conflict marker (migrate writes it
    # when live drift overlapped user edits). One-shot delivery: the
    # frontend acknowledges by reading; we leave the file so it survives
    # accidental tab close, and the frontend POSTs /api/discard?scope=conflicts
    # OR a dedicated dismiss endpoint to clear it. For now read-only delivery.
    if DRAFT_RESET_REASONS.exists():
        try:
            payload["draft_reset_reasons"] = json.loads(
                DRAFT_RESET_REASONS.read_text()
            )
        except (OSError, json.JSONDecodeError):
            pass
    return web.json_response(payload)


async def post_apply(request):
    """alpha.20: atomic, crash-safe Apply. Validates all three drafts using
    the same validators run at PUT (re-validation defends against a draft
    that was edited on disk between PUT and Apply, or a draft that was
    written through an older code path that didn't validate). Snapshots
    LIVE bytes in memory; stages all three drafts as `*.applying` siblings
    via _atomic_write_bytes (each individually crash-safe via fsync); writes
    the journal marker; performs atomic rename to live for each surface;
    deletes the journal; runs render. On render failure, restores live from
    the in-memory snapshots and returns 500 with the stderr.

    Crash-recovery is handled by migrate._recover_apply_journal at boot:
    a partial Apply (some renames done, some pending) is completed; a
    crash before any rename leaves a stale journal that gets cleaned up
    (no actual live mutation occurred yet)."""
    draft_routes_raw = _load_routes_draft()
    draft_mws_raw = _load_middlewares_draft()
    draft_config_raw = _load_config_draft()
    live_routes = _load_routes_yml()
    live_mws = _load_middlewares_yml()

    # Validate (re-canonicalize). Errors surface with {stage, path, message}
    # so the UI can scroll to the offender. The validators raise
    # HTTPBadRequest with a message — convert to a structured error response.
    try:
        validated_routes = _validate_routes(draft_routes_raw)
        _enforce_system_route_protection(validated_routes, live_routes)
        validated_mws = await _validate_middlewares(draft_mws_raw, live_mws)
        validated_mws = _reinject_system_middlewares(validated_mws, live_mws)
        defined_names = {m["name"] for m in validated_mws}
        _cross_reference_middlewares(validated_routes, defined_names)
        validated_config = _validate_config(draft_config_raw)
    except web.HTTPBadRequest as e:
        return web.json_response(
            {"ok": False, "stage": "validate", "error": e.text},
            status=400,
        )

    # No-op guard. If nothing's pending, refuse to apply (avoids
    # spuriously hot-reloading traefik on a double-click).
    rd = _routes_diff(validated_routes, live_routes)
    md = _middlewares_diff(validated_mws, live_mws)
    cd = _config_diff(validated_config, _load_config_yml())
    total = (len(rd["modified"]) + len(rd["added"]) + len(rd["deleted"])
             + len(md["modified"]) + len(md["added"]) + len(md["deleted"])
             + len(cd["modified"]))
    if total == 0:
        return web.json_response(
            {"ok": False, "stage": "noop", "error": "No pending changes."},
            status=409,
        )

    # Sort routes for stable on-disk order (system first, then user).
    validated_routes = sorted(
        validated_routes, key=lambda r: 0 if r.get("system") else 1
    )

    # Acquire apply_lock. The draft_write_lock is acquired briefly inside
    # for the snapshot+stage phases; released before render so other tabs
    # can keep auto-saving while the ~1s render runs.
    apply_lock = request.app[APPLY_LOCK]
    draft_lock = request.app[DRAFT_WRITE_LOCK]
    async with apply_lock:
        live_paths = [ROUTES_YML, MIDDLEWARES_YML, CONFIG_YML]
        async with draft_lock:
            # Snapshot live bytes for rollback.
            snapshots: dict[Path, bytes | None] = {}
            for p in live_paths:
                snapshots[p] = p.read_bytes() if p.exists() else None

            # Stage validated docs as *.applying siblings.
            staged_docs = [
                (ROUTES_YML, {"routes": validated_routes}),
                (MIDDLEWARES_YML, {"version": 1, "middlewares": validated_mws}),
                (CONFIG_YML, validated_config),
            ]
            for live_path, doc in staged_docs:
                applying = live_path.parent / (live_path.name + ".applying")
                _atomic_write_yml(applying, doc)

            # Journal — written AFTER staging so a crash before this point
            # leaves no live mutation. A crash AFTER this point but before
            # all renames complete is recovered on next boot by
            # migrate._recover_apply_journal.
            journal = {
                "targets": [str(p) for p in live_paths],
                "version": ADDON_VERSION,
                "ts": int(time.time()),
            }
            _atomic_write_bytes(
                APPLY_JOURNAL, yaml.safe_dump(journal).encode("utf-8")
            )

            # Atomic renames (os.replace is atomic on POSIX).
            for live_path in live_paths:
                applying = live_path.parent / (live_path.name + ".applying")
                os.replace(str(applying), str(live_path))

            # Renames complete; delete the journal.
            APPLY_JOURNAL.unlink(missing_ok=True)

        # Render (outside draft_lock so concurrent edits aren't blocked
        # during the ~1s render).
        ok, err = await _run_render_with_rollback(snapshots)
        if not ok:
            return web.json_response(
                {"ok": False, "stage": "render", "error": err},
                status=500,
            )

        # Success — update baselines to match the new live so the next
        # _ensure_drafts_consistent run sees no drift.
        for live_path, _draft_path, baseline_path in SURFACE_TRIPLES:
            try:
                _atomic_write_bytes(baseline_path, live_path.read_bytes())
            except OSError as ex:
                sys.stderr.write(
                    f"WARN: failed updating baseline {baseline_path}: {ex}\n"
                )
        # Clear stale conflict marker if any (apply resolves all conflicts).
        DRAFT_RESET_REASONS.unlink(missing_ok=True)

    return web.json_response(
        {"ok": True, "applied": total, "stderr": err}
    )


async def post_discard(request):
    """alpha.20: reset draft to live. Body: {scope: 'all'|'routes'|
    'middlewares'|'config'|'field', path?: 'routes.<rid>.scheme' (only when
    scope=='field')}. Field-scope is reserved for alpha.21; alpha.20 ships
    'all' and per-surface."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    scope = body.get("scope", "all")
    if scope not in {"all", "routes", "middlewares", "config", "field"}:
        raise web.HTTPBadRequest(text=f"unknown scope: {scope!r}")
    if scope == "field":
        # alpha.21 stub: backend not yet implemented.
        raise web.HTTPNotImplemented(
            text="field-scoped discard is alpha.21; use 'all' or per-surface"
        )

    surface_targets = {
        "routes":      (ROUTES_YML, ROUTES_DRAFT_YML),
        "middlewares": (MIDDLEWARES_YML, MIDDLEWARES_DRAFT_YML),
        "config":      (CONFIG_YML, CONFIG_DRAFT_YML),
    }
    if scope == "all":
        targets = list(surface_targets.values())
    else:
        targets = [surface_targets[scope]]

    async with request.app[DRAFT_WRITE_LOCK]:
        for live_path, draft_path in targets:
            if live_path.exists():
                _atomic_write_bytes(draft_path, live_path.read_bytes())
            else:
                draft_path.unlink(missing_ok=True)
        if scope == "all":
            # Clear the stale conflict marker too — a full reset acknowledges
            # whatever the merge surfaced.
            DRAFT_RESET_REASONS.unlink(missing_ok=True)

    return web.json_response({"discarded": scope})


# alpha.20: version-skew middleware. After an addon upgrade, an old browser
# tab still has the previous app.js loaded. Mutating against a new backend
# (which may have new validators or new endpoints) is unsafe. Frontend sends
# X-Addon-Version on every mutating request; mismatch → 409 with a clear
# "reload required" message so the UI can prompt.
GATED_VERSION_METHODS = {"PUT", "POST", "DELETE", "PATCH"}
VERSION_UNGATED_PATHS = {
    # Session ops are unversioned by design — a fresh tab with no app.js
    # cache yet needs to be able to claim or take over.
    "/api/session/claim",
    "/api/session/takeover",
}
# alpha.23: `/api/internal/*` is the cross-addon API surface — called by
# sibling addons over the supervisor's bridge network, NOT by the user's
# browser. Skip the version gate entirely (sibling addons don't know our
# X-Addon-Version) and skip the session gate (callers are addons, not
# users in tabs). Authentication is a non-empty Bearer token in the
# Authorization header — the bridge network is internal-only so we
# trust any container that can reach us.
VERSION_UNGATED_PREFIXES = ("/api/internal/",)


@web.middleware
async def version_gate_mw(request, handler):
    if (request.method in GATED_VERSION_METHODS
            and request.path.startswith("/api/")
            and request.path not in VERSION_UNGATED_PATHS
            and not any(request.path.startswith(p) for p in VERSION_UNGATED_PREFIXES)):
        client_version = request.headers.get("X-Addon-Version", "")
        if client_version and client_version != ADDON_VERSION:
            return web.json_response(
                {"error": f"Addon version mismatch: client={client_version} "
                          f"server={ADDON_VERSION}. Reload required.",
                 "code": "VERSION_MISMATCH"},
                status=409,
            )
    return await handler(request)


async def client_session_ctx(app):
    app[CLIENT] = aiohttp.ClientSession()
    # alpha.20: lock split.
    # - apply_lock: held during POST /api/apply for the entire flow
    #   (snapshot + stage + journal + rename + render + baseline update).
    #   Other apply-touching handlers (post_fix_trusted_proxies,
    #   post_dismiss_integration) also acquire this so they don't race
    #   with Apply.
    # - draft_write_lock: held during PUT-to-draft (cheap, no render) +
    #   briefly inside Apply for the snapshot+stage+rename phase. Released
    #   before the render call so concurrent auto-saves from other tabs
    #   don't hang during the ~1s render window. Within Apply, the
    #   draft_write_lock release after rename is safe -- live is already
    #   the new content and other writers are draft-only.
    app[APPLY_LOCK] = asyncio.Lock()
    app[DRAFT_WRITE_LOCK] = asyncio.Lock()
    # (The `save_lock` back-compat alias that used to be created here is gone:
    # it had zero references and was a plain string key, so keeping it would
    # have meant inventing an AppKey for dead weight. ISSUES.md #5.)
    yield
    await app[CLIENT].close()


def make_app():
    # One Gate per application. Built here, not in the cleanup_ctx, because
    # gate_middleware is a FACTORY that closes over the instance and the
    # middleware list has to be complete before web.Application is constructed.
    gate = Gate(ttl_s=SESSION_TTL)

    # Middleware order: outermost first. json_error_mw wraps ALL responses
    # (including the 423 HTTPLocked the gate raises) as JSON; version_gate_mw
    # rejects stale clients early so neither the gate nor the handler sees a
    # mismatched-version request; the gate runs innermost.
    app = web.Application(
        middlewares=[
            json_error_mw,
            version_gate_mw,
            gate_middleware(GATED_MUTATIONS, gate, message=LOCKED_MESSAGE),
        ]
    )
    app[GATE] = gate
    app.cleanup_ctx.append(client_session_ctx)
    app.router.add_get("/", serve_index)
    app.router.add_static("/static", str(WEB_ROOT / "static"))
    app.router.add_get("/api/routes", get_routes)
    app.router.add_put("/api/routes", put_routes)
    app.router.add_get("/api/middlewares", get_middlewares)
    app.router.add_put("/api/middlewares", put_middlewares)
    app.router.add_get("/api/providers", get_providers)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/status", get_status)
    app.router.add_get("/api/route-health", get_route_health)
    app.router.add_post("/api/restart", post_restart)
    app.router.add_post("/api/restart-core", post_restart_core)
    app.router.add_post("/api/fix-trusted-proxies", post_fix_trusted_proxies)
    app.router.add_post("/api/dismiss-integration", post_dismiss_integration)
    app.router.add_post("/api/session/claim", post_session_claim)
    app.router.add_post("/api/session/takeover", post_session_takeover)
    # alpha.20: draft/live diff + Apply + Discard.
    app.router.add_get("/api/pending", get_pending)
    app.router.add_post("/api/apply", post_apply)
    app.router.add_post("/api/discard", post_discard)
    # alpha.23: cross-addon internal API. Bypasses session + version
    # gates; auth is a non-empty Bearer header (homelab bridge trust).
    app.router.add_post("/api/internal/routes", post_internal_routes)
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
