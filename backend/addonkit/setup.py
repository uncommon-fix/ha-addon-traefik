# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/setup.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""Onboarding completion — the step that ends `NEEDS_SETUP`.

The lifecycle already had a state for "this add-on is not configured yet" and
a verb for "make the draft live". What it did not have is the *transition*
between them, and leaving that to each add-on produced the same bug twice:
the wizard writes the DRAFT, nothing promotes it, and the user lands on a
dashboard whose live config is still the shipped placeholder — with their own
wizard input showing up as anonymous "pending changes" next to whatever else
was pending. Two things follow, and this module exists to make both true:

  * **Completion IS the apply.** Finishing setup is not "save and then please
    also click Apply"; a setup that has not been applied has not happened.
  * **Pending is meaningless while onboarding.** `pending` means
    draft-differs-from-live, but before the first completion `live` is a
    default nobody chose, so diffing against it manufactures noise. Until
    setup completes there is no pending — there is an unfinished setup. That
    is the one line of `pending_is_meaningful`, and it is here rather than in
    `settings.py` because it is a question about the LIFECYCLE, not about the
    store: an add-on with no `Store` (traefik) needs the same answer.

Both shapes are served, and neither is privileged:

    complete_setup(probe=..., store=store, on_apply=reload)   # kit's Store
    complete_setup(probe=..., on_setup_complete=my_apply)     # own model

Requiring `Store` would have given the feature zero consumers on the day it
shipped — the one add-on that has onboarding, traefik, declined `Store`
precisely because its state is three YAML surfaces plus a journal, not one
JSON dict. So the hook path is the first-class one and the `Store` path is the
convenience.

WHAT IS GUARANTEED, and what is not:

  * Refused unless the add-on is in `NEEDS_SETUP` right now. Completing twice,
    or completing an add-on with no onboarding, is a programmer error and
    raises rather than quietly "succeeding".
  * On success the next `probe()` returns something other than `NEEDS_SETUP`
    and, when a `Store` is involved, nothing is pending. Both are CHECKED, not
    assumed: a hook that returns while the add-on is still unconfigured is a
    failure the caller has to hear about.
  * On failure the add-on is still in onboarding and the user's draft is
    untouched, so they can correct and retry. `Store.apply` already restores
    live on a failing `on_apply`; a hook is expected to be equally atomic, and
    the post-condition check is what catches one that is not.

The one thing NOT done is undoing a completion that half-worked. If the action
succeeded but the probe still says `NEEDS_SETUP`, the add-on is — by its own
report — still onboarding with the user's input on disk, which is exactly the
recoverable state we want; rolling live back on top of that would destroy the
input and reload the service a second time to reach a state no better.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable

from .errors import SetupError
from .settings import Store
from .views import NEEDS_SETUP

_LOG = logging.getLogger(__name__)

#: The add-on's state probe, the same one `AddonKit` takes. Sync is accepted so
#: a trivial probe does not need an `async def`.
Probe = Callable[[], "Awaitable[str] | str"]
#: The add-on's own "apply everything" step. Takes nothing: an add-on that
#: brought its own settings model already knows where its draft lives, and
#: handing it a dict the kit does not own would be a lie about who is in
#: charge. May be `async def` (traefik's is) or plain.
SetupHook = Callable[[], "Awaitable[None] | None"]


def pending_is_meaningful(state: str) -> bool:
    """Whether a pending-changes count means anything in `state`.

    False exactly while onboarding. A UI should hide its pending count / Apply
    bar when this is False instead of showing the user their own half-finished
    setup as a diff against a placeholder they never chose.
    """
    return state != NEEDS_SETUP


async def complete_setup(
    *,
    probe: Probe,
    store: Store | None = None,
    on_apply: Callable[[dict], None] | None = None,
    on_setup_complete: SetupHook | None = None,
) -> dict:
    """End onboarding by applying what the wizard wrote. Returns a summary.

    Exactly one action runs. `on_setup_complete` wins when both are given,
    because supplying it is how an add-on says "completion is mine"; with only
    a `store`, the action is `store.apply(on_apply)`.

    Raises `SetupError` — and only `SetupError` — for every refusal and every
    failure, with the original exception chained. The states it refuses in are
    as much a part of the contract as the ones it accepts:

        not in NEEDS_SETUP      -> refused, nothing ran
        neither store nor hook  -> refused, nothing ran (unfinishable)
        the action raised       -> still onboarding, draft intact
        still NEEDS_SETUP after -> the action lied; caller must hear about it
        store still pending     -> ditto, by the other half of the invariant
    """
    if not callable(probe):
        raise SetupError("probe must be callable")
    if on_setup_complete is not None and not callable(on_setup_complete):
        raise SetupError("on_setup_complete must be callable or None")
    if on_setup_complete is None and store is None:
        # An add-on that has onboarding but neither a store nor a hook has no
        # way to finish it. Better to say so than to return "completed".
        raise SetupError(
            "nothing to complete: pass a Store (with its on_apply) or an "
            "on_setup_complete hook"
        )

    before = await _probe_state(probe, "before completing setup")
    if before != NEEDS_SETUP:
        raise SetupError(
            f"setup can only be completed from {NEEDS_SETUP!r}; the add-on is "
            f"in {before!r}. Completing twice, or completing an add-on that "
            f"never onboards, is a programmer error, not a no-op."
        )

    via = "hook" if on_setup_complete is not None else "store"
    try:
        if on_setup_complete is not None:
            await _call(on_setup_complete)
        else:
            assert store is not None  # established above
            # `apply` is synchronous and shells out through on_apply, so it
            # goes off the loop for the same reason AddonKit.handle_apply does.
            await asyncio.to_thread(store.apply, on_apply or (lambda _cfg: None))
    except SetupError:
        raise
    except Exception as exc:
        # Deliberately not re-raising the cause: one exception type out of one
        # function, and the caller's 409 mapping does not have to know that a
        # Store path fails with SettingsError and a hook path with anything.
        raise SetupError(
            f"setup was not completed, the add-on is still onboarding: {exc}"
        ) from exc

    after = await _probe_state(probe, "after completing setup")
    if after == NEEDS_SETUP:
        raise SetupError(
            "the completion step returned successfully but the add-on still "
            f"reports {NEEDS_SETUP!r}. Setup is NOT complete — the draft is "
            "still on disk, so the user can correct it and retry."
        )
    if store is not None:
        left = store.pending()
        if left:
            raise SetupError(
                "setup completed but the store still reports pending changes "
                f"({sorted(left)}); the invariant is that completion leaves "
                "nothing pending."
            )
    _LOG.info("onboarding completed via %s; state is now %r", via, after)
    return {"completed": True, "via": via, "was": before, "state": after}


# ------------------------------------------------------------------ helpers

async def _probe_state(probe: Probe, when: str) -> str:
    """The probe's answer, or `SetupError`.

    House rule 5 says a probe never raises out — but that rule is about a
    best-effort read that decides which PAGE to render, where "unknown" can
    safely mean "show the dashboard". Here the probe gates a mutating,
    unrepeatable step, so the only safe direction is closed: if we cannot
    establish the state, we do not complete setup.
    """
    try:
        value = await _maybe_await(probe())
    except Exception as exc:  # noqa: BLE001 -- see docstring: fail closed
        raise SetupError(f"cannot read the add-on state {when}: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise SetupError(
            f"the probe returned {value!r} {when}, which is not a state name"
        )
    return value


async def _call(fn: Callable[[], Any]) -> None:
    """Await an async hook; run a plain one off the loop.

    A synchronous hook is the one that shells out to a renderer, and a second
    of blocked event loop stalls every other tab's poll — the same reasoning
    `on_apply` gets in `app.py`. An `async def` is already the caller's own
    coroutine and is awaited as-is.
    """
    if inspect.iscoroutinefunction(fn):
        await fn()
        return
    result = await asyncio.to_thread(fn)
    if inspect.isawaitable(result):
        # e.g. a functools.partial around an async def: the coroutine got
        # created on the worker thread but can only be driven on the loop.
        await result


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
