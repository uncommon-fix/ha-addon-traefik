# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/ingress.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""Ingress path whitelisting, view response headers, and template rendering.

All three add-ons grew their own copy of these ~15 lines and the copies had
already drifted: two of them matched the supervisor's ingress token with an
unbounded `+`, and all three chained `str.replace()` calls to substitute
fragments -- which re-scans text that a fragment just contributed. This module
is the unifi copy (the tightened one) generalised, with the re-scan fixed.

No I/O and no supervisor calls: everything here is a pure function of its
arguments, which is why the whole module is testable off-VM.
"""

from __future__ import annotations

import html
import re
from typing import Any

from .errors import KitError

# The supervisor's ingress prefix, and nothing else. The token is base64url,
# so the character class cannot express `/`, `.` or any HTML metacharacter --
# `../` and `"><script>` fail the match rather than being sanitised out.
#
# `\Z` rather than `$` on purpose: Python's `$` also matches immediately before
# a trailing newline, so `$` would accept a header value ending in "\n" and
# smuggle it into the page. The length bound is the second half of the same
# audit finding -- the siblings' `+` accepts a megabyte-long "token".
INGRESS_RE = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]{20,128}/?\Z")

# `{{NAME}}`. Deliberately narrow: a template that writes `{{ NAME }}` or
# `{{name}}` gets no substitution and shows the literal, which is a visible
# bug rather than a silent blank.
_TOKEN_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")


def ingress_path(request: Any) -> str:
    """The validated, HTML-escaped ingress prefix for this request, or "".

    Takes anything with a `.headers` mapping (an `aiohttp.web.Request` in
    production, a stub in tests) so the whitelist can be exercised without a
    server. A header that is absent, malformed, over-long or hostile yields ""
    -- never an exception, because a bad header must degrade to "links are
    relative" and not to a 500.

    The escape is belt and braces: a value that passes INGRESS_RE contains no
    character `html.escape` would touch. It stays so that the guarantee "this
    string is safe inside an HTML attribute" is local to this function and does
    not depend on reading the regex.
    """
    headers = getattr(request, "headers", None)
    raw: Any = ""
    if headers is not None:
        try:
            raw = headers.get("X-Ingress-Path", "")
        except (AttributeError, TypeError):
            raw = ""
    if not isinstance(raw, str) or not INGRESS_RE.match(raw):
        return ""
    return html.escape(raw.rstrip("/"), quote=True)


def view_headers() -> dict[str, str]:
    """Response headers every HTML view gets.

    `frame-ancestors 'self'` because an ingress view is *meant* to be framed,
    by Home Assistant and by nothing else. `no-store` because every view is
    state-dependent: a cached "starting" page outlives the thing it is waiting
    for. A fresh dict each call -- callers mutate what they are handed.
    """
    return {
        "Content-Security-Policy": "frame-ancestors 'self'",
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }


def render(template: str, fragments: dict[str, str], **subs: str) -> str:
    """Substitute `{{KEY}}` tokens in `template`, in ONE pass.

    One pass is the whole point. Chained `str.replace()` calls re-scan the text
    an earlier call inserted, so a fragment that merely *mentions* `{{X}}` --
    in a comment, in sample markup -- has that token expanded, and
    `base-style.html` carries exactly such a comment today (written with the
    braces omitted, as a workaround for this bug). Here a fragment's own text
    is output, never input.

    Fragments win a key collision with `subs`, which is what "fragments are
    substituted first" means once the passes are merged. An unknown token is
    left verbatim so a typo is visible in the page instead of silently blank.
    """
    values: dict[str, str] = dict(subs)
    values.update(fragments or {})

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = values[key]
        return value if isinstance(value, str) else str(value)

    # A function replacement (rather than a string) also stops `\1`, `\g<0>`
    # and friends inside a fragment being interpreted as backreferences.
    return _TOKEN_RE.sub(_replace, template)


def url(ingress: str, path: str) -> str:
    """Join a validated ingress prefix and an app-relative path.

    The server-side twin of `kit.js`'s `url()`. Present because every add-on
    builds this string somewhere and half of them get the double-slash wrong.
    """
    if not isinstance(path, str) or not path:
        raise KitError("url() needs an app-relative path, e.g. '/api/state'")
    if not path.startswith("/"):
        path = "/" + path
    return f"{ingress.rstrip('/')}{path}"
