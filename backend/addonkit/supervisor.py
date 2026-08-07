# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/supervisor.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""Supervisor REST client -- deliberately only the endpoints we may read.

Measured on ha-test with `hassio_role: default`: **only** `/addons/self/info`
and `/addons/<slug>/info` answer 200. `/addons`, `/ingress/panels` and Core's
`/api/hassio/addons` proxy all return 403 even with `hassio_api: true`, and
raising the role to `manager` costs a security-rating point. So there is no
enumerate path in this module and there must never be one -- the older
davinci-resolve client has one and it is dead code that always 403s.

Everything here is a best-effort probe: a non-200 is a normal answer at our
role, not an incident, so `get()` returns None and logs at DEBUG. Nothing in
this module raises.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

_LOG = logging.getLogger(__name__)

DEFAULT_BASE = "http://supervisor"
DEFAULT_TIMEOUT_S = 5.0

#: Env var the supervisor sets in every add-on container.
TOKEN_ENV = "SUPERVISOR_TOKEN"


def _payload(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    """Unwrap the supervisor's `{"result": "ok", "data": {...}}` envelope.

    Callers want the add-on record, not the envelope; every one of the three
    add-ons unwrapped it at the call site and that is exactly the kind of
    repetition this kit exists to delete.
    """
    if not envelope:
        return None
    data = envelope.get("data")
    return data if isinstance(data, dict) else None


class Supervisor:
    """Thin authenticated GET client for `http://supervisor`.

    `token` defaults to `$SUPERVISOR_TOKEN`, read at construction rather than
    at import so tests can set the environment first (house rule 1). Pass an
    explicit `""` to model "no token" -- every call then returns None without
    touching the network.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base: str = DEFAULT_BASE,
        token: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._session = session
        self._base = base.rstrip("/")
        self._token = os.environ.get(TOKEN_ENV, "") if token is None else token
        self._timeout_s = timeout_s

    @property
    def has_token(self) -> bool:
        """Presence only -- the token itself is never logged or returned."""
        return bool(self._token)

    async def get(self, path: str) -> dict[str, Any] | None:
        """GET `path`, or None on ANY non-200, transport error or bad body.

        Quiet by construction: 403 is the EXPECTED answer for most add-on
        endpoints at our role, so a warning here would train people to ignore
        warnings. Never raises -- a probe that can 500 the caller is not a
        probe.
        """
        if not self._token:
            return None
        if not path.startswith("/"):
            path = "/" + path
        try:
            async with self._session.get(
                f"{self._base}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            ) as resp:
                if resp.status != 200:
                    _LOG.debug("supervisor GET %s -> HTTP %s", path, resp.status)
                    return None
                body = await resp.json()
        except Exception:  # noqa: BLE001 -- best-effort probe, see docstring
            _LOG.debug("supervisor GET %s failed", path, exc_info=True)
            return None
        if not isinstance(body, dict):
            _LOG.debug("supervisor GET %s returned %s, not an object", path, type(body))
            return None
        return body

    async def self_info(self) -> dict[str, Any] | None:
        """Our own add-on record. The one add-on endpoint readable at any role,
        which is what makes deriving a sibling's slug possible at all."""
        return _payload(await self.get("/addons/self/info"))

    async def addon_info(self, slug: str) -> dict[str, Any] | None:
        """A named add-on's record, or None -- which usually means 403/404 and
        is indistinguishable from "not installed" at our role, by design."""
        if not slug or "/" in slug or slug.strip() != slug:
            return None
        return _payload(await self.get(f"/addons/{slug}/info"))
