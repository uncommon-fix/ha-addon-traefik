# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/siblings.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""Finding the sibling add-on, and scaffolding a route into its draft.

Three measurements on ha-test shape this module. Each one makes the obvious
simplification wrong, so re-check them before "cleaning this up":

1. **We cannot enumerate.** At `hassio_role: default` only `/addons/<slug>/info`
   is readable (see `supervisor.py`), so a sibling's slug is *derived* from our
   own. A slug is `<prefix>_<directory>` where the prefix is `local` for a local
   install and `sha1(repository_url)[:8]` for a store one; two add-ons shipped
   in the same index carry the same prefix, so `/addons/self/info` tells us the
   sibling's prefix without knowing any repository URL. **Never hardcode a
   hash** -- an earlier version listed `10e3e42a_traefik`, which stopped
   matching the moment the index URL changed and never matched an add-on
   installed from anyone else's index.
2. **Neither side can use names.** A `host_network` add-on uses the HOST's
   resolv.conf, so `local-traefik` does not resolve (gaierror), and it has no
   bridge hostname of its own to be reached at. We reach a sibling at the
   `ip_address` the supervisor reports; a sibling reaches us at the bridge
   gateway, which for a host-networked add-on is our own address on the
   `hassio` interface -- read from the interface, never hardcoded, because the
   subnet is the supervisor's to choose.
3. **The scaffold lands in a DRAFT.** Traefik appends the route and does not
   publish it; the user reviews and applies in Traefik's own UI. That is the
   contract, not an implementation detail -- a sibling add-on silently mutating
   live reverse-proxy config would be a genuinely bad surprise. Nothing this
   module returns may imply otherwise, which is why success carries
   `published: False`.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import struct
import time
from typing import Any, Iterable, MutableMapping, Sequence

import aiohttp

from .errors import KitError
from .supervisor import Supervisor

_LOG = logging.getLogger(__name__)

DEFAULT_DETECT_TTL_S = 60.0

#: Traefik's ingress port, reachable on the supervisor bridge.
TRAEFIK_INTERNAL_PORT = 8080
TRAEFIK_ROUTES_PATH = "/api/internal/routes"
#: First Traefik release carrying TRAEFIK_ROUTES_PATH; a 404 means "older".
TRAEFIK_MIN_VERSION = "0.1.0-alpha.23"

_HOSTNAME_RE = re.compile(r"[A-Za-z0-9.-]+")
_HOSTNAME_MAX = 253

_SIOCGIFADDR = 0x8915

# detect()'s cache hangs off the Supervisor instance rather than a module
# global (house rule 2): one client per app means one cache per app, and tests
# get a clean one for free by constructing a new Supervisor.
_CACHE_ATTR = "_addonkit_sibling_cache"


# ------------------------------ slug derivation ----------------------------

def _dedup(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


async def slug_candidates(sup: Supervisor, suffix: str) -> list[str]:
    """Slugs to try for the add-on whose directory is `suffix`, best first.

    The derived candidate comes from our own prefix; `local_<suffix>` and the
    bare `<suffix>` follow as fallbacks, which is all we can offer for a
    sibling installed from a *different* index than ours. Fix that case by
    adding a fallback, never by raising `hassio_role`.
    """
    candidates: list[str] = []
    me = await sup.self_info()
    if me:
        slug = str(me.get("slug") or "")
        # Split on the FIRST underscore, not the last. Every prefix the
        # supervisor issues -- `local`, `core`, sha1(repo_url)[:8] -- is free
        # of underscores, while a directory name is not (`my_addon`), so the
        # first separator is the reliable one. unifi-controller's rsplit is
        # correct only because its own directory happens to have none.
        if "_" in slug:
            candidates.append(f"{slug.split('_', 1)[0]}_{suffix}")
    candidates.append(f"local_{suffix}")
    candidates.append(suffix)
    return _dedup(candidates)


# --------------------------------- detection -------------------------------

def _cache_for(sup: object) -> MutableMapping[str, tuple[float, dict[str, Any]]]:
    store = getattr(sup, _CACHE_ATTR, None)
    if store is None:
        store = {}
        try:
            setattr(sup, _CACHE_ATTR, store)
        except AttributeError:
            # A __slots__ or frozen client simply does not get caching. Correct,
            # just chattier -- never a failure.
            _LOG.debug("sibling cache not attachable to %r", type(sup))
    return store


async def detect(
    sup: Supervisor,
    suffix: str,
    *,
    ttl_s: float = DEFAULT_DETECT_TTL_S,
    cache: MutableMapping[str, tuple[float, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """`{installed, slug?, version?, ip_address?, state?}` for a sibling.

    Cached per suffix for `ttl_s`, because the UI polls every few seconds and
    each miss costs up to three supervisor round-trips. `ttl_s=0` disables the
    cache. Absent, stopped and forbidden all collapse to `installed: False` --
    at our role they are genuinely indistinguishable, and the right UI response
    to all three is the same: show nothing.
    """
    store = cache if cache is not None else _cache_for(sup)
    now = time.monotonic()
    hit = store.get(suffix)
    if hit is not None and (now - hit[0]) < ttl_s:
        return dict(hit[1])

    found: dict[str, Any] = {"installed": False}
    for slug in await slug_candidates(sup, suffix):
        data = await sup.addon_info(slug)
        if not data:
            continue
        found = {
            "installed": True,
            "slug": data.get("slug") or slug,
            "version": data.get("version"),
            "ip_address": data.get("ip_address") or None,
            "state": data.get("state"),
        }
        break

    store[suffix] = (now, found)
    return dict(found)


# ------------------------------ bridge gateway -----------------------------

def _ioctl_addr(ifname: str) -> str | None:
    """One interface's IPv4 via SIOCGIFADDR, or None.

    `fcntl` is Linux-only and these modules are written and tested on Windows,
    so the import lives in the function: an absent module is "unknown", never
    an ImportError at module scope. Isolated in its own helper so tests can
    substitute it on any platform.
    """
    try:
        import fcntl
    except ImportError:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(
            sock.fileno(),
            _SIOCGIFADDR,
            struct.pack("256s", ifname.encode()[:15]),
        )
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def bridge_gateway(interfaces: Sequence[str] = ("hassio", "docker0")) -> str | None:
    """Our address on the supervisor bridge -- what a sibling must forward to.

    A host-networked add-on shares the host's netns and therefore holds the
    HOST end of that bridge, so the gateway is simply our own address on
    `hassio` (172.30.32.1 on ha-test). Returns None when it cannot be read;
    the caller's job is then to not offer the feature, not to error.
    """
    for ifname in interfaces:
        try:
            addr = _ioctl_addr(ifname)
        except Exception:  # noqa: BLE001 -- best-effort probe
            _LOG.debug("reading %s failed", ifname, exc_info=True)
            continue
        if addr:
            return addr
    return None


# ------------------------------ route scaffold -----------------------------

def _fail(code: str, error: str, status: int | None = None) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error, "status": status}


async def scaffold_traefik_route(
    session: aiohttp.ClientSession,
    *,
    traefik_ip: str,
    hostname: str,
    backend_host: str,
    backend_port: int,
    scheme: str = "http",
    tls: bool = True,
    source: str = "",
    token: str | None = None,
    traefik_port: int = TRAEFIK_INTERNAL_PORT,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Append a route to Traefik's DRAFT. It is not published; see module docs.

    Returns a result dict rather than raising an HTTP error, because choosing
    the status code is the add-on's policy, not the kit's (house rule 4):

        {"ok": True,  "code": "OK", "status": 200, "name", "rid", "message",
         "backend": "host:port", "published": False}
        {"ok": False, "code": <below>, "status": int|None, "error": str}

    Codes: BAD_HOSTNAME, UNAUTHORIZED, OUTDATED (Traefik predates the endpoint),
    EXISTS (hostname already in the draft), UPSTREAM, NOT_REACHABLE, TIMEOUT.
    `error` is Traefik's own wording whenever it supplied one -- its 409 text is
    better than anything we could invent.
    """
    if not isinstance(backend_port, int) or isinstance(backend_port, bool) \
            or not 1 <= backend_port <= 65535:
        # A bad port is the add-on's bug, not the user's: raise (house rule 5).
        raise KitError(f"backend_port must be 1-65535, got {backend_port!r}")

    hostname = hostname.strip()
    if not hostname or len(hostname) > _HOSTNAME_MAX \
            or not _HOSTNAME_RE.fullmatch(hostname):
        return _fail(
            "BAD_HOSTNAME",
            "Hostname may only contain letters, numbers, dots and hyphens.",
        )
    if not traefik_ip:
        return _fail("NOT_REACHABLE", "Traefik has no address to reach it at.")

    # Traefik's internal endpoint requires a non-empty Authorization header
    # (homelab bridge trust) and 401s without one.
    bearer = os.environ.get("SUPERVISOR_TOKEN", "") if token is None else token
    url = f"http://{traefik_ip}:{traefik_port}{TRAEFIK_ROUTES_PATH}"
    payload = {
        "name": hostname,
        "backend_kind": "external",
        "backend_host": backend_host,
        "backend_port": backend_port,
        "scheme": scheme,
        "tls": tls,
        "source": source,
    }

    try:
        async with session.post(
            url,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            status = resp.status
            body: Any = None
            try:
                body = await resp.json()
            except Exception:  # noqa: BLE001 -- non-JSON error bodies happen
                body = None
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("error") or "")

            if status == 401:
                return _fail(
                    "UNAUTHORIZED",
                    detail or "Traefik rejected the request: no bearer token.",
                    status,
                )
            if status == 404:
                return _fail(
                    "OUTDATED",
                    detail or "This Traefik is too old for cross-add-on routes. "
                              f"Update it to {TRAEFIK_MIN_VERSION} or later.",
                    status,
                )
            if status == 409:
                return _fail(
                    "EXISTS",
                    detail or f"Traefik already has a route named {hostname}.",
                    status,
                )
            if status >= 400:
                text = detail
                if not text:
                    try:
                        text = (await resp.text())[:300]
                    except Exception:  # noqa: BLE001
                        text = ""
                return _fail("UPSTREAM", f"Traefik returned {status}: {text}", status)
    # TimeoutError first: aiohttp.ServerTimeoutError is BOTH a ClientError and
    # a TimeoutError, and "it did not answer" is the more useful of the two.
    except TimeoutError:
        return _fail("TIMEOUT", "Traefik did not respond in time.")
    except aiohttp.ClientError:
        _LOG.debug("scaffold POST %s failed", url, exc_info=True)
        return _fail(
            "NOT_REACHABLE",
            f"Could not reach Traefik at {traefik_ip}:{traefik_port}.",
        )

    result = body if isinstance(body, dict) else {}
    return {
        "ok": True,
        "code": "OK",
        "status": status,
        "name": result.get("name") or hostname,
        "rid": result.get("rid"),
        "message": result.get("message")
        or "Route scaffolded in Traefik's draft. Open Traefik and Apply to publish it.",
        "backend": f"{backend_host}:{backend_port}",
        # Load-bearing: the route is NOT live. Do not drop this field.
        "published": False,
    }
