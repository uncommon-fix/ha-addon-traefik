# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/app.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""The wrapper: an `AddonKit` you construct, and `build()` gives you an app.

This is the only module allowed to know about all the others, and it is
deliberately the least interesting one in the kit. It owns the add-on
*lifecycle* -- pick a view, expose the settings verbs, hold the editor lock,
answer "is the sibling there" -- and nothing else. The domain stays the
add-on's:

    kit = AddonKit(probe=my_probe, views=my_views, settings=store, version=V)
    app = kit.build()
    app.router.add_get("/api/routes", my_handler)   # ours, not the kit's

OPTIONALITY IS THE DESIGN. Everything except `probe` and `views` may be
omitted, and omitted means **the route is never registered**. An add-on with no
`Store` does not get an `/api/settings` that 500s or returns `{}`; it gets a
404, because the capability genuinely does not exist. That is house rule 4 --
the kit provides mechanism and the add-on decides what it has -- and it is why
`build()` assembles a route list rather than registering a fixed table and
branching inside the handlers.

One thing here is deliberately not delegated to the module that looks like it
should own it: **a richer `export|restore` CLI lives here** as well as in
`persist.py`. `python -m addonkit.persist` is the contract's spelling and takes
an explicit `--files`; `python -m addonkit.app --kit module:ATTR` reads the file
list off the AddonKit itself, so an add-on can declare it once in Python
instead of again in its s6 `finish` script. See `main()`.

The gate middleware is NOT here: `gate.gate_middleware` keys on the registered
route pattern per the CONTRACT.md ruling, which is what this module needs, so
it is imported rather than reimplemented.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

import aiohttp
from aiohttp import web

from .errors import KitError, SettingsError
from .gate import SESSION_HEADER, Gate, gate_middleware
from .ingress import ingress_path, view_headers
from .persist import Mirror
from .settings import Store
from .siblings import bridge_gateway, detect, scaffold_traefik_route
from .supervisor import Supervisor
from .views import READY, Views

_LOG = logging.getLogger(__name__)

#: What a probe returns. Async in every real add-on; a plain function is
#: accepted too so a trivial probe does not need an `async def`.
Probe = Callable[[], "Awaitable[str] | str"]

# What the kit puts on the Application, for the add-on's own handlers to reach.
# `web.AppKey` rather than the plain strings the three add-ons use today
# (a plain `"client"` string key): aiohttp warns on string keys, and a typed
# key cannot silently collide with an add-on's own entry of the same name.
CLIENT: web.AppKey[aiohttp.ClientSession] = web.AppKey("addonkit.client")
SUPERVISOR: web.AppKey[Any] = web.AppKey("addonkit.supervisor")
KIT: web.AppKey["AddonKit"] = web.AppKey("addonkit.kit")
VERSION: web.AppKey[str] = web.AppKey("addonkit.version")
SETTINGS: web.AppKey[Any] = web.AppKey("addonkit.settings")
GATE: web.AppKey[Any] = web.AppKey("addonkit.gate")

# scaffold_traefik_route() returns a result dict rather than raising, because
# choosing a status code is the caller's policy (house rule 4). This endpoint
# is the caller, so the policy is here, in one table.
_SCAFFOLD_STATUS: dict[str, int] = {
    "BAD_HOSTNAME": 400,     # the user's typing
    "EXISTS": 409,           # the draft already has that hostname
    "OUTDATED": 409,         # Traefik predates the endpoint: user must update
    "UNAUTHORIZED": 502,     # our bearer token, not theirs -- our bug
    "UPSTREAM": 502,
    "NOT_REACHABLE": 502,
    "TIMEOUT": 504,
}


# --------------------------------------------------------------- middleware

def json_error_middleware(
    *, logger: logging.Logger | None = None
) -> Callable[..., Awaitable[web.StreamResponse]]:
    """Every failure leaves as `{"error": "..."}` with a sensible status.

    The shape all three add-ons already emit, so the frontend never branches on
    content type. Three rules worth stating:

      * an `HTTPException` keeps its own status -- that is how `HTTPLocked`
        arrives as a 423 in the standard shape, and why the gate middleware is
        registered INSIDE this one;
      * `SettingsError` is 409 and every other `KitError` is 400. A handler
        that knows better re-raises as the exact status it wants (a schema
        rejection on PUT is a 400, not a conflict); this is the backstop;
      * anything else is logged in full HERE and answered with a fixed string.
        A traceback in the browser tells an attacker the paths on disk, and
        tells the user nothing they can act on.
    """
    log = logger if logger is not None else _LOG

    @web.middleware
    async def _mw(request: web.Request, handler: Callable) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException as exc:
            # 2xx/3xx "exceptions" (redirects) are control flow, not errors.
            if exc.status < 400 or exc.content_type == "application/json":
                raise
            body = exc.text or exc.reason or f"HTTP {exc.status}"
            return web.json_response({"error": body}, status=exc.status)
        except SettingsError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except KitError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001 -- the whole point of a backstop
            log.exception("unhandled error in %s %s", request.method, request.path)
            return web.json_response({"error": "internal error"}, status=500)

    return _mw


# ------------------------------------------------------------------ helpers

async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _json_object(request: web.Request, *, required: bool = True) -> dict:
    """The request body as a JSON object. 400 on anything else.

    `required=False` turns "no body at all" into `{}`, which is what an
    argument-less POST (claim, takeover) should accept from a `fetch()` that
    sent nothing.
    """
    raw = await request.read()
    if not raw.strip():
        if required:
            raise web.HTTPBadRequest(text="A JSON object body is required.")
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise web.HTTPBadRequest(text=f"Invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Payload must be a JSON object.")
    return body


def _truthy(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class TraefikRoute:
    """What `POST /api/siblings/traefik/route` should ask Traefik to publish.

    Present or absent decides whether the endpoint exists at all. `backend_host`
    is what TRAEFIK must forward to, which is not this add-on's own idea of its
    address: left as None it is read off the supervisor bridge, which is the
    correct answer for a host-networked add-on and the only one that survives
    the supervisor changing its subnet. A str pins it; a callable defers it to
    request time.
    """

    backend_port: int
    backend_host: str | Callable[[], str | None] | None = None
    scheme: str = "http"
    tls: bool = True
    source: str = ""


# ------------------------------------------------------------------ the kit

class AddonKit:
    """The wrapper. Construct it, call `build()`, add your own routes.

    Only `probe` and `views` are required. Every other argument is a capability
    the add-on either has or does not, and the ones it does not have are absent
    from the router rather than present and broken.

        probe       async () -> "starting" | "needs_setup" | "ready". Its
                    vocabulary is the add-on's; the kit only passes it to
                    `Views`, which falls back to READY for anything it has no
                    template for. A probe that raises is logged and treated as
                    READY -- house rule 5, a probe never 500s a page.
        views       a `Views`, or the `mapping` for one (then `web_root` is
                    required and `fragments` is used). Given a `Views`, that
                    instance wins and `web_root` is recorded only for the
                    add-on's own use.
        settings    a `Store`. Registers the six settings routes.
        on_apply    the add-on's reload hook, passed to `Store.apply()` and --
                    per the ruling -- to `Store.rollback()`, so a rollback
                    reloads the service instead of only restoring the file. It
                    runs in a worker thread (a reload shells out and would
                    otherwise stall every other tab's poll), so it must be a
                    plain function; an `async def` is refused at construction
                    rather than silently never awaited.
        redact      applied to every settings dict that leaves the process.
                    See `_clean`.
        gate        a `Gate`. Registers claim/takeover and gates the kit's own
                    mutating routes; `gated` adds the add-on's.
        persist     a `Mirror`, or the filenames for one. No routes -- the
                    mirror runs from shell (see `main()`); it lives on the kit
                    so the file list is declared exactly once.
        siblings    add-on directory suffixes to detect, e.g. `("traefik",)`.
        traefik_route  a `TraefikRoute`. Registers the scaffold endpoint and
                    implies `"traefik"` in `siblings`.
        subs        extra `{{TOKEN}}` values for the index view; sync or async,
                    called per request.
        session / supervisor  injected for tests; otherwise the kit owns a
                    `ClientSession` for the app's lifetime and builds a
                    `Supervisor` over it.
    """

    def __init__(
        self,
        *,
        probe: Probe,
        views: Views | Mapping[str, str],
        web_root: Path | str | None = None,
        fragments: Mapping[str, str] | None = None,
        version: str = "dev",
        settings: Store | None = None,
        on_apply: Callable[[dict], None] | None = None,
        redact: Callable[[dict], dict] | None = None,
        gate: Gate | None = None,
        gated: Iterable[tuple[str, str]] = (),
        persist: Mirror | Sequence[str] | None = None,
        data_dir: Path | str | None = None,
        config_dir: Path | str | None = None,
        siblings: Sequence[str] = (),
        traefik_route: TraefikRoute | None = None,
        subs: Callable[[web.Request], Any] | None = None,
        session: aiohttp.ClientSession | None = None,
        supervisor: Supervisor | None = None,
    ) -> None:
        if not callable(probe):
            raise KitError("probe must be callable")
        self._probe = probe

        if isinstance(views, Views):
            self.views = views
            self.web_root = Path(web_root) if web_root is not None else views.web_root
        else:
            if web_root is None:
                raise KitError("web_root is required when views is a mapping")
            self.web_root = Path(web_root)
            self.views = Views(self.web_root, dict(views), dict(fragments or {}))

        self.version = str(version)

        if settings is not None and not isinstance(settings, Store):
            raise KitError("settings must be a Store or None")
        self.settings = settings
        if on_apply is not None:
            if not callable(on_apply):
                raise KitError("on_apply must be callable or None")
            if inspect.iscoroutinefunction(on_apply):
                # It is handed to Store, which calls it synchronously; a
                # coroutine would be created, dropped, and the apply would
                # "succeed" having reloaded nothing.
                raise KitError(
                    "on_apply must be a plain function -- it runs off the "
                    "event loop, so it cannot be an `async def`"
                )
        self._on_apply = on_apply
        if redact is not None and not callable(redact):
            raise KitError("redact must be callable or None")
        self._redact = redact

        if gate is not None and not isinstance(gate, Gate):
            raise KitError("gate must be a Gate or None")
        self.gate = gate
        extra_gated = {(str(m).upper(), str(p)) for m, p in gated}
        if extra_gated and gate is None:
            # Silently leaving them unguarded is exactly the failure the gate
            # exists to prevent.
            raise KitError(
                f"gated routes were listed but no gate was given: {sorted(extra_gated)}"
            )
        self._extra_gated = extra_gated

        if persist is None or isinstance(persist, Mirror):
            self.mirror = persist
        else:
            self.mirror = Mirror(
                list(persist),
                **{
                    k: Path(v)
                    for k, v in (("data_dir", data_dir), ("config_dir", config_dir))
                    if v is not None
                },
            )

        suffixes = [str(s) for s in siblings]
        if traefik_route is not None:
            if not isinstance(traefik_route, TraefikRoute):
                raise KitError("traefik_route must be a TraefikRoute or None")
            if "traefik" not in suffixes:
                suffixes.append("traefik")
        self.siblings = tuple(dict.fromkeys(suffixes))
        self.traefik_route = traefik_route

        if subs is not None and not callable(subs):
            raise KitError("subs must be callable or None")
        self._subs = subs

        self._session = session
        self._supervisor = supervisor
        # One writer at a time through the store. The gate already stops two
        # BROWSERS colliding; this stops two requests from the same session
        # interleaving a draft write with an apply.
        self._lock = asyncio.Lock()

    # -- building -----------------------------------------------------------

    def build(self) -> web.Application:
        """A wired `aiohttp` Application. Add your own routes to `.router`."""
        routes: list[tuple[str, str, Callable]] = [
            ("GET", "/", self.handle_index),
            ("GET", "/api/state", self.handle_state),
        ]
        gated: set[tuple[str, str]] = set(self._extra_gated)

        if self.settings is not None:
            routes += [
                ("GET", "/api/settings", self.handle_get_settings),
                ("PUT", "/api/settings", self.handle_put_settings),
                ("GET", "/api/settings/pending", self.handle_pending),
                ("POST", "/api/apply", self.handle_apply),
                ("POST", "/api/discard", self.handle_discard),
                ("POST", "/api/rollback", self.handle_rollback),
            ]
            gated |= {
                ("PUT", "/api/settings"),
                ("POST", "/api/apply"),
                ("POST", "/api/discard"),
                ("POST", "/api/rollback"),
            }

        if self.gate is not None:
            # Both ungated on purpose: a freshly opened tab holds no SID and
            # still has to be able to claim, or to take over from a stale
            # holder. Gating these would make the lock unrecoverable.
            routes += [
                ("POST", "/api/session/claim", self.handle_claim),
                ("POST", "/api/session/takeover", self.handle_takeover),
            ]

        if self.siblings:
            routes.append(("GET", "/api/siblings", self.handle_siblings))
        if self.traefik_route is not None:
            routes.append(
                ("POST", "/api/siblings/traefik/route", self.handle_traefik_route)
            )
            gated.add(("POST", "/api/siblings/traefik/route"))

        middlewares: list[Callable] = [json_error_middleware()]
        if self.gate is not None:
            # Inside the JSON middleware, so a 423 is an {"error": ...} body
            # like everything else. `gated` holds route PATTERNS, not concrete
            # paths -- gate.gate_middleware keys on the matched resource's
            # canonical form, so ("DELETE", "/api/libraries/{name}") gates
            # every name.
            middlewares.append(gate_middleware(gated, self.gate))

        app = web.Application(middlewares=middlewares)
        app[KIT] = self
        app[VERSION] = self.version
        app[SETTINGS] = self.settings
        app[GATE] = self.gate
        app.cleanup_ctx.append(self._lifecycle)
        for method, path, handler in routes:
            app.router.add_route(method, path, handler)
        return app

    async def _lifecycle(self, app: web.Application):
        """One `ClientSession` per app, closed on shutdown -- the idiom the
        three add-ons already share. An injected session is borrowed, never
        closed: the test that owns it decides when it dies."""
        owned = self._session is None
        app[CLIENT] = aiohttp.ClientSession() if owned else self._session
        app[SUPERVISOR] = (
            self._supervisor
            if self._supervisor is not None
            else Supervisor(app[CLIENT])
        )
        try:
            yield
        finally:
            if owned:
                await app[CLIENT].close()

    # -- state --------------------------------------------------------------

    async def state(self) -> str:
        """The probe's answer, or READY if it could not give one.

        House rule 5: a probe never raises out. Falling back to READY rather
        than to STARTING is the same rule `Views.template_for` already applies
        to an unknown state -- show the dashboard, do not invent a wizard.
        """
        try:
            value = await _maybe_await(self._probe())
        except Exception:  # noqa: BLE001 -- see docstring
            _LOG.warning(
                "probe raised; treating the add-on as %r", READY, exc_info=True
            )
            return READY
        if not isinstance(value, str) or not value:
            _LOG.warning("probe returned %r, not a state name; using %r", value, READY)
            return READY
        return value

    async def handle_index(self, request: web.Request) -> web.Response:
        state = await self.state()
        extra: dict[str, str] = {}
        if self._subs is not None:
            given = await _maybe_await(self._subs(request)) or {}
            extra = {str(k): str(v) for k, v in given.items()}
        # The kit's own tokens are applied last: INGRESS_PATH is the validated,
        # escaped one and must not be replaceable by an add-on's dict.
        extra["INGRESS_PATH"] = ingress_path(request)
        extra["APP_VERSION"] = self.version
        resp = web.Response(
            text=self.views.render(state, **extra), content_type="text/html"
        )
        resp.headers.update(view_headers())
        return resp

    async def handle_state(self, request: web.Request) -> web.Response:
        return web.json_response({"state": await self.state(), "version": self.version})

    # -- settings -----------------------------------------------------------

    def _clean(self, data: dict) -> dict:
        """Every settings dict on its way out of the process goes through here.

        House rule 6. `redact` is the add-on's, because only it knows which of
        its keys is a Cloudflare token; the kit's job is to make sure there is
        no read path that bypasses it. A redact that returns a non-dict is a
        hard error rather than a fallback to the raw dict -- failing closed is
        the only safe direction here.
        """
        if self._redact is None:
            return data
        out = self._redact(dict(data))
        if not isinstance(out, dict):
            raise KitError("redact() must return a dict")
        return out

    def _clean_pending(self, pending: dict) -> dict:
        """`Store.pending()`, redacted, without lying about what changed.

        Redacting live and draft and diffing afterwards would hide a secret
        changing (both sides mask to the same string, so the key looks
        unmodified). Instead the two sides are redacted as settings-shaped
        dicts and mapped back, so a changed secret still shows as modified with
        both values masked. A key the redactor drops entirely disappears.
        """
        if self._redact is None or not pending:
            return pending
        modified = pending.get("modified") or {}
        live_side = self._clean({k: v.get("live") for k, v in modified.items()})
        draft_side = self._clean({k: v.get("draft") for k, v in modified.items()})
        return {
            "added": self._clean(dict(pending.get("added") or {})),
            "deleted": self._clean(dict(pending.get("deleted") or {})),
            "modified": {
                k: {"live": live_side.get(k), "draft": draft_side.get(k)}
                for k in modified
                if k in live_side or k in draft_side
            },
        }

    def _store(self) -> Store:
        store = self.settings
        if store is None:  # unreachable: the routes are not registered
            raise KitError("no settings store")
        return store

    async def handle_get_settings(self, request: web.Request) -> web.Response:
        store = self._store()
        return web.json_response({
            "live": self._clean(store.live()),
            "draft": self._clean(store.draft()),
            "pending": self._clean_pending(store.pending()),
            "can_rollback": store.can_rollback(),
        })

    async def handle_pending(self, request: web.Request) -> web.Response:
        store = self._store()
        pending = store.pending()
        return web.json_response({
            "pending": self._clean_pending(pending),
            "has_pending": bool(pending),
            "can_rollback": store.can_rollback(),
        })

    async def handle_put_settings(self, request: web.Request) -> web.Response:
        store = self._store()
        patch = await _json_object(request)
        merge = _truthy(request.query.get("merge"))
        async with self._lock:
            try:
                draft = await asyncio.to_thread(store.put_draft, patch, merge=merge)
            except SettingsError as exc:
                # A rejected patch is the caller's input, not a state conflict.
                raise web.HTTPBadRequest(text=str(exc)) from exc
            pending = store.pending()
        return web.json_response({
            "draft": self._clean(draft),
            "pending": self._clean_pending(pending),
            "can_rollback": store.can_rollback(),
        })

    async def handle_apply(self, request: web.Request) -> web.Response:
        store = self._store()
        async with self._lock:
            try:
                # Off the loop: on_apply shells out to reload a service, and a
                # second-long block would stall every other tab's state poll.
                live = await asyncio.to_thread(
                    store.apply, self._on_apply or (lambda _cfg: None)
                )
            except SettingsError as exc:
                raise web.HTTPConflict(text=str(exc)) from exc
            pending = store.pending()
        return web.json_response({
            "live": self._clean(live),
            "pending": self._clean_pending(pending),
            "can_rollback": store.can_rollback(),
        })

    async def handle_discard(self, request: web.Request) -> web.Response:
        store = self._store()
        async with self._lock:
            try:
                draft = await asyncio.to_thread(store.discard)
            except SettingsError as exc:
                raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({
            "draft": self._clean(draft),
            "pending": {},
            "can_rollback": store.can_rollback(),
        })

    async def handle_rollback(self, request: web.Request) -> web.Response:
        store = self._store()
        async with self._lock:
            try:
                # The additive on_apply from the ruling. Without it the file
                # goes back and the service keeps running the bad config --
                # the exact failure rollback exists to undo, one step later.
                live = await asyncio.to_thread(store.rollback, self._on_apply)
            except SettingsError as exc:
                raise web.HTTPConflict(text=str(exc)) from exc
            pending = store.pending()
        return web.json_response({
            "live": self._clean(live),
            "pending": self._clean_pending(pending),
            "can_rollback": store.can_rollback(),
        })

    # -- session ------------------------------------------------------------

    def _gate(self) -> Gate:
        gate = self.gate
        if gate is None:  # unreachable: the routes are not registered
            raise KitError("no gate")
        return gate

    async def handle_claim(self, request: web.Request) -> web.Response:
        gate = self._gate()
        body = await _json_object(request, required=False)
        sid = str(body.get("sid") or request.headers.get(SESSION_HEADER, "") or "")
        new_sid, claimed = gate.claim(sid or None)
        # A refusal is 200, not an error: "someone else is editing, they were
        # last seen N seconds ago" is the answer the takeover prompt needs, and
        # the sid is "" so a reader cannot impersonate the holder.
        return web.json_response(
            {"sid": new_sid, "claimed": claimed, "age_s": gate.age_s()}
        )

    async def handle_takeover(self, request: web.Request) -> web.Response:
        gate = self._gate()
        return web.json_response({"sid": gate.takeover(), "claimed": True})

    # -- siblings -----------------------------------------------------------

    def _backend_host(self) -> str | None:
        spec = self.traefik_route
        if spec is None:
            return None
        host = spec.backend_host
        if host is None:
            return bridge_gateway()
        if callable(host):
            try:
                host = host()
            except Exception:  # noqa: BLE001 -- best-effort probe
                _LOG.debug("backend_host callable failed", exc_info=True)
                return None
        return str(host) if host else None

    async def handle_siblings(self, request: web.Request) -> web.Response:
        sup = request.app[SUPERVISOR]
        found = {suffix: await detect(sup, suffix) for suffix in self.siblings}
        body: dict[str, Any] = {"siblings": found}
        if self.traefik_route is not None:
            host = self._backend_host()
            traefik = found.get("traefik") or {}
            body["backend"] = {"host": host, "port": self.traefik_route.backend_port}
            # One flag for the UI to branch on, so the reason a banner is
            # absent stays server-side and explainable.
            body["can_scaffold"] = bool(
                traefik.get("installed") and traefik.get("ip_address") and host
            )
        return web.json_response(body)

    async def handle_traefik_route(self, request: web.Request) -> web.Response:
        spec = self.traefik_route
        assert spec is not None  # the route is not registered without one
        body = await _json_object(request)
        hostname = str(body.get("name") or "").strip()
        if not hostname:
            raise web.HTTPBadRequest(text="Enter a hostname for the route.")

        info = await detect(request.app[SUPERVISOR], "traefik")
        if not info.get("installed"):
            raise web.HTTPConflict(text="The Traefik add-on is not installed.")
        traefik_ip = info.get("ip_address")
        if not traefik_ip:
            raise web.HTTPConflict(
                text="Traefik is installed but not running -- start it and try again."
            )
        host = self._backend_host()
        if not host:
            raise web.HTTPConflict(
                text="Could not determine this add-on's address on the Home "
                     "Assistant bridge, so Traefik would have nothing to "
                     "forward to."
            )

        result = await scaffold_traefik_route(
            request.app[CLIENT],
            traefik_ip=str(traefik_ip),
            hostname=hostname,
            backend_host=host,
            backend_port=spec.backend_port,
            scheme=spec.scheme,
            tls=spec.tls,
            source=spec.source,
        )
        if not result.get("ok"):
            status = _SCAFFOLD_STATUS.get(str(result.get("code")), 502)
            return web.json_response(
                {"error": result.get("error") or "Traefik refused the route.",
                 "code": result.get("code")},
                status=status,
            )
        return web.json_response(result)


# ------------------------------------------------------------- the CLI seam
# The mirror runs from an s6 `finish` script and from cont-init -- from shell,
# with no event loop and no web app -- so it needs an entry point that is not a
# handler. It lives here rather than in persist.py for one reason: `--kit`
# resolves an AddonKit and mirrors ITS file list, so the list is declared once,
# in the add-on's server module, instead of once there and once in the shell.
#
# Replaces rootfs/usr/local/bin/state-sync.sh, called the same way:
#     python3 -m addonkit.app export --kit unifi_server:KIT
#     python3 -m addonkit.app restore --files "routes.yml config.yml acme.json"


def _load_attr(spec: str) -> Any:
    """`package.module:ATTRIBUTE` -> the attribute."""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise KitError(f"--kit wants 'module:attribute', got {spec!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise KitError(f"cannot import {module_name!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise KitError(f"{module_name!r} has no attribute {attr!r}") from exc


def _mirror_from_args(args: argparse.Namespace) -> Mirror:
    if args.kit:
        obj = _load_attr(args.kit)
        # Duck-typed on purpose: run as `python -m addonkit.app` this module is
        # also `__main__`, so an AddonKit built by an imported add-on is an
        # instance of a *different* class object with the same name.
        mirror = getattr(obj, "mirror", obj)
        if not isinstance(mirror, Mirror):
            raise KitError(f"{args.kit} is not a Mirror and has no .mirror")
        return mirror

    names: list[str] = list(args.file or [])
    for blob in args.files or []:
        names.extend(part for part in blob.replace(",", " ").split() if part)
    if not names:
        raise KitError("nothing to mirror: pass --kit, --file or --files")
    dirs = {
        k: Path(v)
        for k, v in (("data_dir", args.data_dir), ("config_dir", args.config_dir))
        if v is not None
    }
    return Mirror(names, **dirs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m addonkit.app",
        description="Mirror an add-on's state between /data and /config.",
    )
    parser.add_argument("action", choices=("export", "restore"))
    parser.add_argument(
        "--kit", metavar="MODULE:ATTR",
        help="an AddonKit (or Mirror) to take the file list and paths from",
    )
    parser.add_argument("--file", action="append", metavar="NAME",
                        help="a tracked filename; repeatable")
    parser.add_argument("--files", action="append", metavar="LIST",
                        help="tracked filenames, whitespace- or comma-separated")
    parser.add_argument("--data-dir", metavar="DIR", help="default /data")
    parser.add_argument("--config-dir", metavar="DIR", help="default /config")
    # Worth having: the ways a mirror does nothing -- /config not mapped, live
    # state already in /data, no stamp to restore from -- are all logged at
    # DEBUG by persist.py, so without this a no-op export is indistinguishable
    # from a successful one at the exit code.
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log the reasons a file was skipped")
    args = parser.parse_args(argv)

    # Configured here, never at import: a library that grabs the root logger on
    # import is a library that fights its host.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[addonkit] %(message)s",
    )

    try:
        mirror = _mirror_from_args(args)
    except KitError as exc:
        parser.error(str(exc))          # exits 2
    try:
        if args.action == "export":
            mirror.export()
        else:
            mirror.restore()
    except Exception:  # noqa: BLE001 -- a finish script must not crash the stop
        _LOG.exception("%s failed", args.action)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
