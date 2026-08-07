# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/views.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""State -> view selection, with shared HTML fragments substituted in.

The three add-ons landed on the same shape independently -- a probe returns a
state name, and exactly one template is honest about that state. unifi keeps
the mapping as a table rather than an `if` chain, deliberately, so that adding
a state cannot silently reuse whichever template the old chain fell through
to. That table is what this generalises.

Two states are OPTIONAL and the kit must not pretend otherwise:

  * `NEEDS_SETUP` -- davinci has nothing to configure. An add-on that omits it
    never enters that state, and must never be handed a fake wizard.
  * `STARTING` -- an add-on whose service is up the moment the container is
    has nothing to wait for.

`READY` is the only required entry: it is also the fallback, so an unforeseen
state renders the dashboard instead of 500ing the page.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .errors import KitError
from .ingress import render as _render

_LOG = logging.getLogger(__name__)

STARTING = "starting"
NEEDS_SETUP = "needs_setup"
READY = "ready"


class Views:
    """A web root, a state->template table, and a token->fragment table.

    Both tables hold FILENAMES resolved under `web_root`, never file contents:
    house rule 1 forbids reading anything at import or construction time, and
    reading per request is also what lets a template be edited on a running
    dev container without a restart. Templates are small and the OS page cache
    makes the re-read free.
    """

    def __init__(
        self,
        web_root: Path,
        mapping: dict[str, str],
        fragments: dict[str, str] | None = None,
    ) -> None:
        self.web_root = Path(web_root)
        mapping = dict(mapping or {})
        if READY not in mapping:
            # Without READY there is no fallback, so an unforeseen state would
            # have nowhere to go -- the one thing this class exists to prevent.
            raise KitError(
                f"views mapping must define {READY!r}; got {sorted(mapping)}"
            )
        self.mapping = mapping
        self.fragments = dict(fragments or {})

    # ------------------------------------------------------------------

    def template_for(self, state: str) -> str:
        """The template filename for `state`, falling back to READY's.

        Covers both the unforeseen state and the deliberately omitted optional
        one with the same rule: whatever we cannot name, we show the dashboard
        for. An add-on that omits NEEDS_SETUP therefore cannot be routed into a
        wizard it does not have, even by a buggy probe.
        """
        return self.mapping.get(state, self.mapping[READY])

    def has_state(self, state: str) -> bool:
        """Whether `state` has a view of its own. Lets a probe skip work for a
        state the add-on has opted out of, rather than computing it and having
        `template_for` throw it away."""
        return state in self.mapping

    def render(self, state: str, **subs: str) -> str:
        """The rendered HTML for `state`. Fragments first, then `subs`."""
        template = self._read_template(self.template_for(state))
        fragments = {
            token: self._read_fragment(name)
            for token, name in self.fragments.items()
        }
        return _render(template, fragments, **subs)

    # ------------------------------------------------------------------

    def _resolve(self, name: str) -> Path:
        """`name` as a path under `web_root`, or KitError.

        Template names come from the add-on's own code, not from a request, so
        this is a typo guard rather than a security boundary -- but it costs
        one comparison and it turns `../../etc/passwd` into a clear error.
        """
        root = self.web_root.resolve()
        target = (root / name).resolve()
        if target != root and root not in target.parents:
            raise KitError(f"template {name!r} escapes web root {self.web_root}")
        return target

    def _read_template(self, name: str) -> str:
        """A missing template is fatal: it is a packaging error in the image,
        and rendering the wrong page would hide it."""
        try:
            return self._resolve(name).read_text(encoding="utf-8")
        except OSError as exc:
            raise KitError(f"could not read template {name!r}: {exc}") from exc

    def _read_fragment(self, name: str) -> str:
        """A missing fragment degrades to nothing, per house rule 5 -- a
        fragment is an optional capability (the Traefik banner is literally
        one), and a banner that fails to load must make the page plainer, not
        blank."""
        try:
            return self._resolve(name).read_text(encoding="utf-8")
        except (OSError, KitError):
            _LOG.debug("fragment %r unavailable; rendering without it", name)
            return ""
