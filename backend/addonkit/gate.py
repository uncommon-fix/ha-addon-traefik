# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/gate.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""One editor at a time.

The UI is opened in several tabs and browsers; the server allows ONE active
edit session. Mutating endpoints require the caller's `X-Session-Id` to match
the current session, and a mismatch is **423 Locked, never 403** -- 403 reads
as an authentication failure and sends people to look for the wrong problem.
A second tab is offered "take over" (which invalidates the holder) or
"view read-only" (no SID, all mutations refused).

Heartbeat rather than a heartbeat endpoint: any request carrying a matching
`X-Session-Id` refreshes the session, so the UI's existing few-second state
poll keeps it alive on its own. After `ttl_s` without one the session expires
server-side and the next claim succeeds. An expired session is NOT revivable
by a late heartbeat -- otherwise a background poll from a closed laptop would
hold the lock forever.

Modelled on `ha-addon-traefik/backend/server.py`'s SessionManager, which is the
original; davinci-resolve carries a copy documented as a copy.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from aiohttp import web

from .errors import GateError

DEFAULT_TTL_S = 120.0
SESSION_HEADER = "X-Session-Id"
LOCKED_MESSAGE = (
    "Another tab or browser is editing this add-on. Reload to claim a new "
    "session, or take over."
)

# 24 bytes of urlsafe randomness: a stale SID colliding with a fresh one is not
# a case worth writing code for.
_SID_BYTES = 24


class HTTPLocked(web.HTTPException):
    """423 Locked. aiohttp ships no HTTPLocked, so subclass HTTPException
    directly; an add-on's JSON error middleware wraps it like any other."""

    status_code = 423


@dataclass
class _Session:
    sid: str
    last_seen: float


class Gate:
    """The single-editor lock.

    `clock` exists so expiry is testable without sleeping; it defaults to
    `time.monotonic`, which -- unlike `time.time` -- cannot be moved by an NTP
    step, so a clock correction can never silently expire or extend a session.
    """

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(ttl_s, (int, float)) or ttl_s <= 0:
            raise GateError(f"ttl_s must be a positive number, got {ttl_s!r}")
        self._ttl_s = float(ttl_s)
        self._clock = clock if clock is not None else time.monotonic
        self._current: _Session | None = None

    # -- internals ---------------------------------------------------------

    def _expire(self, now: float) -> None:
        if self._current is not None and now - self._current.last_seen > self._ttl_s:
            self._current = None

    # -- API ---------------------------------------------------------------

    def claim(self, sid: str | None = None) -> tuple[str, bool]:
        """Become the active editor. Returns `(sid, claimed)`.

        Passing the SID you already hold is a no-op refresh that succeeds --
        without that, a UI path that re-claims while already holding (a reload
        after "discard changes", say) would lock itself out.

        On failure the returned sid is `""`, never the holder's: handing out
        the live SID would let any reader impersonate the editor. Use
        `age_s()` for how stale the holder looks, which is what the takeover
        prompt actually needs.
        """
        now = self._clock()
        self._expire(now)
        if self._current is None:
            new_sid = secrets.token_urlsafe(_SID_BYTES)
            self._current = _Session(sid=new_sid, last_seen=now)
            return new_sid, True
        if sid and sid == self._current.sid:
            self._current.last_seen = now
            return self._current.sid, True
        return "", False

    def takeover(self) -> str:
        """Forcibly become the active editor, invalidating any holder.

        Deliberately unconditional and deliberately ungated at the HTTP layer:
        a freshly opened tab holds no SID and still has to be able to do this.
        """
        now = self._clock()
        sid = secrets.token_urlsafe(_SID_BYTES)
        self._current = _Session(sid=sid, last_seen=now)
        return sid

    def is_current(self, sid: str | None) -> bool:
        """True only for the live holder's SID. Empty/None is always False."""
        if not sid:
            return False
        now = self._clock()
        self._expire(now)
        return self._current is not None and self._current.sid == sid

    def touch(self, sid: str | None) -> None:
        """Heartbeat. Refreshes only on a match, and expiry is checked first so
        a late beat cannot resurrect a session someone else may already own."""
        if not sid:
            return
        now = self._clock()
        self._expire(now)
        if self._current is not None and self._current.sid == sid:
            self._current.last_seen = now

    def age_s(self) -> float | None:
        """Seconds since the holder's last heartbeat, or None when nobody
        holds the gate. Drives the takeover prompt's "idle for N minutes"."""
        now = self._clock()
        self._expire(now)
        if self._current is None:
            return None
        return now - self._current.last_seen


_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _route_key(request: web.Request) -> str:
    """The registered pattern for this request, e.g. "/api/libraries/{name}".

    aiohttp exposes it as the matched resource's canonical form. An unmatched
    request (404 in flight) has no resource, so fall back to the concrete path
    rather than treating it as ungated.
    """
    resource = getattr(getattr(request, "match_info", None), "route", None)
    resource = getattr(resource, "resource", None)
    canonical = getattr(resource, "canonical", None)
    return canonical if isinstance(canonical, str) and canonical else request.path


def gate_middleware(
    gated: Iterable[tuple[str, str]],
    gate: Gate,
    *,
    header: str = SESSION_HEADER,
    message: str = LOCKED_MESSAGE,
) -> Callable[[web.Request, _Handler], Awaitable[web.StreamResponse]]:
    """Build the middleware guarding `gated` (method, route pattern) pairs.

    A factory, not a middleware itself -- it has to close over the Gate and the
    route set. Register it INSIDE any JSON-error middleware so the 423 comes
    back in the same shape as every other error.

    Each entry is the pattern the developer WROTE, so a dynamic route is one
    entry: `("DELETE", "/api/libraries/{name}")` gates every name, while a
    concrete `("DELETE", "/api/libraries/films")` gates nothing once the route
    is dynamic. Matching is otherwise exact -- reads, the claim/takeover
    endpoints, static assets and any cross-add-on bridge endpoint stay ungated,
    and listing writers explicitly means a new endpoint is unguarded until
    someone thinks about it, rather than guarded by accident.
    """
    routes = frozenset(gated)

    @web.middleware
    async def _gate(request: web.Request, handler: _Handler) -> web.StreamResponse:
        sid = request.headers.get(header, "")
        if sid:
            gate.touch(sid)
        # Match the REGISTERED ROUTE PATTERN, not the concrete path, so a
        # dynamic route is expressible: DELETE /api/libraries/{name} gates
        # every name without a prefix convention nobody can see. Falls back
        # to request.path so an unrouted request cannot slip past.
        if (request.method, _route_key(request)) in routes \
                and not gate.is_current(sid):
            raise HTTPLocked(text=message)
        return await handler(request)

    return _gate
