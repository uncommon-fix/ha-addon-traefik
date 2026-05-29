"""Render Traefik static + dynamic YAML from /data/config.yml + /data/routes.yml.

Run from cont-init on every container start and re-run by the backend after each
Save in the add-on's Web UI. /data/options.json is only a back-compat fallback
for pre-0.5 installs; the Web UI (state in /data/*.yml) is the source of truth.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

OPTIONS = Path("/data/options.json")
# Phase D: routes live in /data/routes.yml (managed by backend UI). Phase B/C
# back-compat: if the file is missing or empty, fall back to options['routes'].
ROUTES_IN = Path("/data/routes.yml")
# Phase D follow-up (0.5.0): core config (provider, cloudflare_token,
# acme_email, domain) lives in /data/config.yml managed by the Setup tab.
# Same back-compat fallback to /data/options.json fields.
CONFIG_IN = Path("/data/config.yml")
# Phase F (0.7.0): middleware definitions managed by the Middlewares tab.
MIDDLEWARES_IN = Path("/data/middlewares.yml")
STATIC_OUT = Path("/etc/traefik/traefik.yml")
DYNAMIC_DIR = Path("/etc/traefik/dynamic")
ROUTES_OUT = DYNAMIC_DIR / "routes.yml"

# HA Core's internal hostname on the supervisor's hassio Docker network.
# Resolves via CoreDNS regardless of homeassistant_api flag.
HA_INTERNAL_HOST = "homeassistant"
HA_INTERNAL_PORT = 8123

# alpha.7 / alpha.14: shared serversTransport for routes with skip_tls_verify
# set (was a magic-string middleware attachment pre-alpha.14; now a per-route
# bool). SERVERS_TRANSPORT_REF must be @file-qualified — a bare name resolves
# to @internal and Traefik errors. The map KEY under http.serversTransports is
# the bare name; only the service's reference is qualified.
SERVERS_TRANSPORT_NAME = "insecure-skip-verify"
SERVERS_TRANSPORT_REF = "insecure-skip-verify@file"

# Traefik uses '@' for provider-qualification (cloudflare@file); spaces/dots
# break the YAML-key + name-lookup path. Restrict the resolver name to a safe
# subset that Traefik unambiguously accepts as a map key and a cross-reference.
RESOLVER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

YAML_HEADER = (
    "# Managed by the traefik add-on - do not edit by hand.\n"
    "# Re-rendered from /data/config.yml + /data/routes.yml on every start.\n"
)


def _resolve_health_path(route: dict[str, Any]) -> str:
    # Phase E: per-route healthCheck.path override. Backend validator enforces
    # the leading '/' but render.py also runs from cont-init on a routes.yml
    # that may be hand-edited; coerce instead of fail-fast so one typo doesn't
    # kill the whole add-on.
    raw = (route.get("health_path") or "").strip()
    if not raw:
        return "/"
    if not raw.startswith("/"):
        print(
            f"WARN: health_path {raw!r} missing leading '/'; coercing",
            file=sys.stderr,
        )
        raw = "/" + raw
    return raw


def _load_routes(opts: dict[str, Any]) -> list:
    # Phase D source-of-truth: /data/routes.yml written by the backend's UI.
    # Fall back to options['routes'] if the file is missing (pre-migration) or
    # unreadable. Empty file/list is a valid "no routes" state.
    if ROUTES_IN.exists():
        try:
            parsed = yaml.safe_load(ROUTES_IN.read_text()) or {}
            return parsed.get("routes") or []
        except yaml.YAMLError as err:
            print(
                f"WARN: cannot parse {ROUTES_IN}: {err}; "
                "falling back to options['routes']",
                file=sys.stderr,
            )
    return opts.get("routes") or []


def _load_core_config(opts: dict[str, Any]) -> dict[str, Any]:
    # Phase D follow-up source-of-truth: /data/config.yml written by the
    # Setup tab. Falls back to supervisor options for Phase A-C back-compat.
    # Mirrors backend's _load_config_yml semantics so the renderer and the
    # UI see identical effective config.
    merged: dict[str, Any] = {
        "provider": "cloudflare",
        "cloudflare_token": "",
        "acme_email": "",
        "domain": "",
        "ha_hostname": "hass",
        "entrypoint_http": "web",
        "entrypoint_https": "websecure",
        "log_level": "INFO",
        "acme_resolver": "cloudflare",
        "force_ssl": False,
    }
    if opts.get("acme_resolver"):
        merged["acme_resolver"] = opts["acme_resolver"]
        merged["provider"] = opts["acme_resolver"]
    for field in ("cloudflare_token", "acme_email", "domain", "ha_hostname",
                  "entrypoint_http", "entrypoint_https", "log_level"):
        if opts.get(field):
            merged[field] = opts[field]
    if "force_ssl" in opts:
        merged["force_ssl"] = bool(opts["force_ssl"])
    if CONFIG_IN.exists():
        try:
            data = yaml.safe_load(CONFIG_IN.read_text()) or {}
            for field in ("provider", "cloudflare_token", "acme_email",
                          "domain", "ha_hostname", "entrypoint_http",
                          "entrypoint_https", "log_level"):
                if field in data and data[field] is not None:
                    merged[field] = data[field]
            if "force_ssl" in data and data["force_ssl"] is not None:
                merged["force_ssl"] = bool(data["force_ssl"])
            # acme_resolver name in Traefik = provider name in config.yml.
            if data.get("provider"):
                merged["acme_resolver"] = data["provider"]
        except yaml.YAMLError as err:
            print(
                f"WARN: cannot parse {CONFIG_IN}: {err}; "
                "falling back to options",
                file=sys.stderr,
            )
    return merged


def _acme_active(opts: dict[str, Any]) -> bool:
    # NEVER store or log the token value. Truthiness + strip handles "", None,
    # missing-key, AND whitespace-only. The actual token reaches Traefik via
    # CF_DNS_API_TOKEN env var set in cont-init.
    config = _load_core_config(opts)
    email = (config.get("acme_email") or "").strip()
    token = (config.get("cloudflare_token") or "").strip()
    return bool(email) and bool(token)


def main() -> int:
    # /data/options.json is a back-compat fallback only; with no supervisor
    # schema it may be absent or {}. Treat missing as empty; FATAL only on a
    # present-but-corrupt file (mirrors backend's _load_config_yml).
    if OPTIONS.exists():
        try:
            opts = json.loads(OPTIONS.read_text())
        except (OSError, json.JSONDecodeError) as err:
            print(f"FATAL: cannot read options.json: {err}", file=sys.stderr)
            return 1
    else:
        opts = {}

    config = _load_core_config(opts)
    resolver_name = (config.get("acme_resolver") or "").strip()
    if resolver_name and not RESOLVER_NAME_RE.match(resolver_name):
        print(
            f"FATAL: acme_resolver {resolver_name!r} must match "
            f"{RESOLVER_NAME_RE.pattern} (lowercase, digits, '-' or '_'; "
            "no '@' or spaces).",
            file=sys.stderr,
        )
        return 1

    if resolver_name and not _acme_active(opts):
        missing = [
            k for k in ("acme_email", "cloudflare_token")
            if not (config.get(k) or "").strip()
        ]
        print(
            f"WARN: certResolver {resolver_name!r} requested but "
            f"{missing} missing in config - TLS routes will use Traefik's "
            "self-signed cert until you add them via the Setup tab.",
            file=sys.stderr,
        )

    static = _build_static(opts)
    dynamic, n_enabled, n_skipped = _build_dynamic(opts)

    DYNAMIC_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_OUT.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(STATIC_OUT, YAML_HEADER + _dump(static))

    # Traefik v3.7 rejects both `http: {}` and `http: {routers: {}, services: {}}`
    # as "standalone elements". When there are zero enabled routes, write NO
    # routes.yml at all — Traefik's file provider tolerates an empty dynamic
    # directory and just loads zero routers/services. Unlink any prior file.
    if n_enabled > 0:
        _atomic_write(ROUTES_OUT, YAML_HEADER + _dump(dynamic))
    elif ROUTES_OUT.exists():
        ROUTES_OUT.unlink()

    print(f"rendered {n_enabled} routes ({n_skipped} skipped)", file=sys.stderr)
    return 0


def _http_entrypoint(config: dict[str, Any]) -> dict[str, Any]:
    """The :80 entrypoint. When force_ssl is on, attach a global redirection to
    the https entrypoint (permanent/301), so EVERY http request upgrades before
    routing — superseding the per-route redirect-to-https middleware. DNS-01 ACME
    is unaffected (it never uses :80; an HTTP-01 switch would need to exempt
    /.well-known/acme-challenge/ from this)."""
    ep: dict[str, Any] = {"address": ":80"}
    if config.get("force_ssl"):
        ep["http"] = {
            "redirections": {
                "entryPoint": {
                    "to": config["entrypoint_https"],
                    "scheme": "https",
                    "permanent": True,
                }
            }
        }
    return ep


def _build_static(opts: dict[str, Any]) -> dict[str, Any]:
    # 0.5.3: log_level + entrypoint names come from /data/config.yml (Advanced
    # tab) with supervisor-options fallback for Phase A-C back-compat.
    config_full = _load_core_config(opts)
    static: dict[str, Any] = {
        "log": {"level": config_full.get("log_level", "INFO")},
        # accessLog: {} (empty map) enables CLF-format access logs to stdout
        # per Traefik v3 docs; the supervisor captures stdout for the add-on Log.
        "accessLog": {},
        "entryPoints": {
            config_full["entrypoint_http"]: _http_entrypoint(config_full),
            config_full["entrypoint_https"]: {"address": ":443"},
            # Phase D: localhost-only entryPoint for Traefik's own dashboard.
            # `api.insecure: true` below auto-attaches the dashboard router to
            # the reserved entryPoint name `traefik`. Bound to 127.0.0.1 so
            # only our backend's reverse-proxy can reach it; HA ingress auth
            # gates that path.
            "traefik": {"address": "127.0.0.1:8090"},
        },
        "providers": {
            "file": {"directory": str(DYNAMIC_DIR), "watch": True},
        },
        # Phase D: enable the read-only dashboard via the reserved entryPoint.
        # `insecure: true` is safe here because the entryPoint is localhost-
        # bound (see entryPoints.traefik above).
        "api": {"dashboard": True, "insecure": True},
    }

    # Phase C: ACME resolver block only emitted when both email + token are
    # configured. Otherwise routes fall back to Phase B's self-signed tls: {}.
    # Phase D follow-up: core config now sourced from /data/config.yml.
    if _acme_active(opts):
        static["certificatesResolvers"] = {
            config_full["acme_resolver"]: {
                "acme": {
                    "email": config_full["acme_email"].strip(),
                    "storage": "/data/acme.json",
                    "dnsChallenge": {
                        "provider": "cloudflare",
                        "resolvers": ["1.1.1.1:53", "1.0.0.1:53"],
                    },
                },
            },
        }
    return static


def _build_dynamic(opts: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    routers: dict[str, Any] = {}
    services: dict[str, Any] = {}
    # slug -> hostname for collision detection. slug = hostname.replace(".", "-")
    # is many-to-one (`a.b.lan` and `a-b.lan` both -> `a-b-lan`); Traefik silently
    # last-wins inside a dict, so detect + skip the second occurrence and treat
    # the collision as a config bug.
    seen_slugs: dict[str, str] = {}
    enabled = skipped = 0
    # alpha.7: emit the shared insecure-skip-verify serversTransport only if a
    # route actually references it (keeps the dynamic file minimal).
    uses_skip_transport = False

    # Optional shared apex: when `domain` is set, route `hostname` fields are
    # treated as bare subdomain labels (e.g. "cloud" + "example.com" ->
    # "cloud.example.com"). Use "@" or empty hostname to route the apex itself.
    # When `domain` is empty, hostname is a full FQDN (Phase B behaviour).
    # Phase D follow-up: domain comes from /data/config.yml first.
    config = _load_core_config(opts)
    domain = (config.get("domain") or "").strip().lower()
    force_ssl = bool(config.get("force_ssl"))

    # Phase F (0.7.0): the HA self-route is no longer renderer-injected here.
    # It now lives as a real, persisted route in /data/routes.yml marked with
    # system: ha_self (seeded by migrate.py on upgrade). The existing route
    # loop below renders it identically to user routes -- the backend_kind
    # auto-fill branch handles the homeassistant:8123 target.
    #
    # Phase D: routes primarily live in /data/routes.yml (managed by backend UI).
    # If the file is missing or empty, fall back to options['routes'] (Phase B/C
    # back-compat). The backend's migrate.py runs once at cont-init to copy
    # options.routes -> /data/routes.yml on first boot of v0.4.0.
    routes_input = _load_routes(opts)

    # Defence-in-depth third layer of the system-route ordering invariant:
    # migrate.py inserts system routes at
    # index 0; backend's _validate_routes sorts system-first on save; the
    # renderer sorts here too so that a corrupted file or hand-edit can't make
    # a user route's slug collide-and-win over the system route.
    routes_input = sorted(routes_input, key=lambda r: 0 if r.get("system") else 1)

    # Middleware name -> type map. A `redirectScheme` middleware on a TLS route
    # is applied via a dedicated web-entrypoint redirect router (so http gets a
    # 308); everything else stays on the main router. Loaded once and reused for
    # the http.middlewares block below.
    mw_raw = _load_middlewares()
    mw_type = {m["name"]: m.get("type") for m in mw_raw if m.get("name")}

    for route in routes_input:
        if not route.get("enabled", True):
            skipped += 1
            continue

        raw_hostname = route["hostname"].strip().lower()

        if "*" in raw_hostname:
            # Wildcard certs require tls.domains config (out of Phase C scope);
            # the bare Host(`*.foo`) matcher rejects wildcards.
            print(
                f"WARN: skipping route {raw_hostname!r}: wildcard hostnames "
                "require tls.domains (deferred - future phase)",
                file=sys.stderr,
            )
            skipped += 1
            continue

        if domain:
            if raw_hostname in ("", "@"):
                hostname = domain
            elif "." in raw_hostname:
                print(
                    f"WARN: skipping route {raw_hostname!r}: when 'domain' is "
                    f"set ({domain!r}), per-route hostname must be a bare "
                    "subdomain label (no dots). Use '@' or '' for the apex.",
                    file=sys.stderr,
                )
                skipped += 1
                continue
            else:
                hostname = f"{raw_hostname}.{domain}"
        else:
            hostname = raw_hostname

        slug = hostname.replace(".", "-")

        if slug in seen_slugs:
            print(
                f"WARN: skipping route {hostname!r}: slug {slug!r} collides "
                f"with already-rendered route {seen_slugs[slug]!r}",
                file=sys.stderr,
            )
            skipped += 1
            continue

        if route["backend_kind"] == "home_assistant":
            backend_host = HA_INTERNAL_HOST
            backend_port = HA_INTERNAL_PORT
            scheme = "http"
        else:
            backend_host = (route.get("backend_host") or "").strip()
            backend_port = route.get("backend_port")
            scheme = route.get("scheme", "https")
            if not backend_host or not backend_port:
                print(
                    f"WARN: skipping route {hostname!r}: external backend "
                    "requires backend_host + backend_port",
                    file=sys.stderr,
                )
                skipped += 1
                continue

        tls = bool(route.get("tls", True))
        # force_ssl redirects ALL :80 traffic to :443 at the entrypoint, before
        # routing. An http-only (tls:false) route would be redirected to https
        # where it has no websecure router -> 404. Skip it loudly rather than
        # serve a redirect-to-nowhere.
        if force_ssl and not tls:
            print(
                f"WARN: skipping http-only route {hostname!r}: force_ssl is on "
                "(all http redirects to https) but this route has tls disabled, "
                "so it has no HTTPS router. Enable tls on the route or turn off "
                "Force SSL.",
                file=sys.stderr,
            )
            skipped += 1
            continue
        route_mws = [m for m in (route.get("middlewares") or []) if m]
        # Split by resolved type (order-preserving), two ways:
        #  - redirectScheme: drives a separate web-entrypoint redirect router
        #    (keeping redirect OFF the websecure router avoids an https->https
        #    308 loop);
        #  - everything else stays on the main router, in order.
        # alpha.14: skip-TLS-verify is no longer a middleware — read the bool
        # at route level and apply directly to the service transport below.
        redirect_mws = [m for m in route_mws if mw_type.get(m) == "redirectScheme"]
        other_mws = [m for m in route_mws if mw_type.get(m) != "redirectScheme"]

        router: dict[str, Any] = {
            "rule": f"Host(`{hostname}`)",
            "entryPoints": [
                config["entrypoint_https"] if tls else config["entrypoint_http"]
            ],
            "service": slug,
        }
        if other_mws:
            router["middlewares"] = other_mws
        if tls:
            if _acme_active(opts):
                router["tls"] = {"certResolver": config["acme_resolver"]}
            else:
                # Phase B fallback: built-in self-signed cert. Browser warns.
                router["tls"] = {}

        routers[slug] = router

        # HTTP->HTTPS redirect: a TLS route carrying a redirectScheme middleware
        # gets a second router on the web (:80) entrypoint applying ONLY the
        # redirect middleware (308 to https before the service is reached). A
        # non-TLS route ignores redirect middlewares — redirecting an http-only
        # route to https would break it.
        if tls and redirect_mws and not force_ssl:
            redirect_slug = f"{slug}-redirecthttps"
            if redirect_slug in routers or redirect_slug in seen_slugs:
                print(
                    f"WARN: redirect router {redirect_slug!r} collides with an "
                    "existing route; skipping http->https redirect",
                    file=sys.stderr,
                )
            else:
                routers[redirect_slug] = {
                    "rule": f"Host(`{hostname}`)",
                    "entryPoints": [config["entrypoint_http"]],
                    "middlewares": redirect_mws,
                    "service": slug,
                }
        load_balancer: dict[str, Any] = {
            "servers": [
                {"url": f"{scheme}://{backend_host}:{backend_port}"}
            ],
            # Per-service healthCheck so /api/http/services populates
            # serverStatus (the reachability integration depends on this).
            # Per-route override via the optional `health_path` field.
            "healthCheck": {
                "path": _resolve_health_path(route),
                "interval": "30s",
                "timeout": "5s",
            },
        }
        # alpha.14: per-route skip-TLS-verify bool -> service serversTransport
        # that disables backend cert verification (lets a route front a
        # self-signed HTTPS backend). No-op on http backends. Coexists with
        # healthCheck. (Was a magic-string middleware attachment pre-alpha.14.)
        if bool(route.get("skip_tls_verify")):
            load_balancer["serversTransport"] = SERVERS_TRANSPORT_REF
            uses_skip_transport = True
        services[slug] = {"loadBalancer": load_balancer}
        seen_slugs[slug] = hostname
        enabled += 1

    # Phase F (0.7.0): translate /data/middlewares.yml into Traefik's
    # http.middlewares block. WARN-log any route that references an undefined
    # middleware (the backend validator rejects this at save time; this WARN
    # covers the hand-edited routes.yml path).
    middlewares_defs = _build_middlewares(mw_raw)
    defined_names = set(middlewares_defs)
    for slug, router in routers.items():
        for mw_name in router.get("middlewares", []) or []:
            if mw_name not in defined_names:
                print(
                    f"WARN: route {slug!r} references undefined middleware "
                    f"{mw_name!r}; Traefik will reject the router",
                    file=sys.stderr,
                )

    # Traefik v3.7 rejects `routers: {}` (empty map) with "routers cannot be a
    # standalone element". Omit empty subkeys so the zero-routes file is just
    # `http: {}` — a valid no-op dynamic config. Same treatment for middlewares.
    http_config: dict[str, Any] = {}
    if routers:
        http_config["routers"] = routers
    if services:
        http_config["services"] = services
    if middlewares_defs:
        http_config["middlewares"] = middlewares_defs
    # alpha.7: define the shared insecure-skip-verify transport only when a
    # service references it (the map key is the bare name; services reference it
    # as insecure-skip-verify@file).
    if uses_skip_transport:
        http_config["serversTransports"] = {
            SERVERS_TRANSPORT_NAME: {"insecureSkipVerify": True}
        }
    return {"http": http_config}, enabled, skipped


def _load_middlewares() -> list[dict[str, Any]]:
    if not MIDDLEWARES_IN.exists():
        return []
    try:
        parsed = yaml.safe_load(MIDDLEWARES_IN.read_text()) or {}
        return parsed.get("middlewares") or []
    except yaml.YAMLError as err:
        print(
            f"WARN: cannot parse {MIDDLEWARES_IN}: {err}",
            file=sys.stderr,
        )
        return []


def _build_middlewares(defs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Translate the addon's normalised middleware shape into Traefik's
    http.middlewares block."""
    out: dict[str, dict[str, Any]] = {}
    for mw in defs:
        name = mw.get("name")
        typ = mw.get("type")
        cfg = mw.get("config") or {}
        if not name or not typ:
            continue
        if typ == "basicAuth":
            # Traefik wants users as ["user:hash", ...]; drop incomplete entries.
            users_line = [
                f"{u['username']}:{u['password_hash']}"
                for u in (cfg.get("users") or [])
                if u.get("username") and u.get("password_hash")
            ]
            if users_line:
                out[name] = {"basicAuth": {"users": users_line}}
        elif typ == "ipAllowList":
            ranges = cfg.get("sourceRange") or []
            if ranges:
                out[name] = {"ipAllowList": {"sourceRange": ranges}}
        elif typ == "redirectScheme":
            out[name] = {"redirectScheme": {
                "scheme": cfg.get("scheme", "https"),
                "permanent": bool(cfg.get("permanent", True)),
            }}
        elif typ == "headers":
            # Traefik SETS empty-value headers to empty (NOT delete). To
            # delete, the user removes the row in the UI -> entry drops out of
            # the saved map. We additionally filter empty KEYS here in case
            # the UI sent a blank in-progress row.
            req = {k: v for k, v in (cfg.get("customRequestHeaders") or {}).items() if k}
            res = {k: v for k, v in (cfg.get("customResponseHeaders") or {}).items() if k}
            headers_block: dict[str, Any] = {}
            if req:
                headers_block["customRequestHeaders"] = req
            if res:
                headers_block["customResponseHeaders"] = res
            if headers_block:
                out[name] = {"headers": headers_block}
    return out


def _dump(data: dict[str, Any]) -> str:
    return yaml.safe_dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=False,
        width=4096,
    )


def _atomic_write(target: Path, content: str) -> None:
    # Traefik's file provider filters by extension (.toml/.yaml/.yml) per
    # pkg/provider/file/file.go — the .tmp transient file is ignored during
    # the brief window before os.replace renames it into place.
    # Crash-safe: flush + fsync the data, atomic rename, then fsync the
    # parent dir. Mirrors server.py/migrate.py so a kernel crash mid-write
    # can't leave a zero-byte /etc/traefik/*.yml.
    tmp = target.parent / (target.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    fd = os.open(str(target.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
