# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/settings.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""Three-layer settings store: live, draft, baseline.

Generalised from `ha-addon-traefik/backend/server.py`, which is the only place
in the workspace that already has a staged-edit model that works. See
`ha-addon-traefik/docs/data-state-model.md` for the original.

    live      what the service is running. ONLY apply() writes it.
    draft     what the user is editing. Every put_draft() writes it.
    baseline  the previous live, captured by apply() BEFORE live is touched.

Three verbs, deliberately distinct:

    discard()   draft <- live       "forget what I typed"
    apply()     live  <- draft,     "make it so"
                baseline <- previous live
    rollback()  live  <- baseline   "that apply was a mistake"

Rollback semantics, which are new here and were a decision rather than a
transcription:

  * **The draft is not touched.** After an apply the draft equals live, so at
    the moment of a rollback the draft is the only remaining copy of the
    content being undone. Throwing it away would lose the user's work with no
    second undo. The visible consequence is intended: right after a rollback
    `pending()` is non-empty and the UI honestly reads "live is the old
    config, your editor still holds the change that failed". `discard()` is
    how you clear it, and that keeps the three verbs orthogonal — only
    discard writes the draft from live, only apply writes baseline.
  * **You cannot roll back twice.** Baseline is one deep; there is no history
    stack. A successful rollback consumes the baseline, `can_rollback()` then
    returns False, and a second call raises. The alternative — re-arming
    baseline from the live we just replaced — would look like an undo history
    while actually flip-flopping between two states, which is worse than not
    having one.
  * **`can_rollback()` is False before any apply.** No apply means no
    baseline, and "roll back to nothing" is destruction, not recovery.

Serialisation is **JSON from the stdlib**. Traefik keeps its own state as YAML
(plus JSON for `options.json` and the reset-reason marker), but the kit is
vendored into all three add-ons and the unifi image installs only
`python3 python3-aiohttp` — it has no YAML module at all, and adding one to
three images to store a flat dict is not a trade worth making. The public
surface is `dict` either way, so the on-disk format is an implementation
detail. Output is sorted and indented so equal dicts always produce equal
bytes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from .errors import SettingsError

_LOG = logging.getLogger(__name__)

_ENCODING = "utf-8"


# ---------------------------------------------------------------- disk I/O

def _dumps(data: dict[str, Any]) -> bytes:
    # sort_keys makes the bytes a function of the dict alone, so a re-write of
    # unchanged content is a genuine no-op and tests can compare bytes.
    try:
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"settings are not JSON-serialisable: {exc}") from exc
    return (text + "\n").encode(_ENCODING)


def _fsync_dir(directory: Path) -> None:
    # Persists the rename itself, not just the file contents: without it a
    # power loss between os.replace and writeback can leave the directory
    # entry pointing at the old inode. Best-effort by design — Windows has no
    # directory handle you can fsync through os.open, so this is a no-op on
    # the dev workstation and the real guarantee only exists on the target.
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write `payload` to `path` so that a kill at any instant leaves either
    the old file or the new one, never a truncated one.

    Modelled on traefik's `_atomic_write_bytes`. The temp file is created in
    the destination directory (rename is only atomic within a filesystem) with
    a unique name, so two concurrent writers cannot scribble on each other's
    staging file the way a fixed `<name>.tmp` allows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Settings can hold credentials (traefik's config carries a Cloudflare
        # token), so never widen past the owner. No-op on Windows.
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        # Never leave staging litter behind on a failed write.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _read(path: Path) -> dict[str, Any] | None:
    """Parsed contents, or None when the file does not exist.

    A file that exists but cannot be parsed is a hard error: silently
    returning {} would make apply() diff against a phantom empty config and
    quietly wipe a live service.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw.decode(_ENCODING))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SettingsError(
            f"{path} must hold a JSON object, found {type(parsed).__name__}"
        )
    return parsed


def _read_bytes_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc


def _restore(path: Path, prior: bytes | None) -> None:
    """Put `path` back exactly as it was, including "it did not exist"."""
    if prior is None:
        path.unlink(missing_ok=True)
        _fsync_dir(path.parent)
    else:
        _atomic_write(path, prior)


# ------------------------------------------------------------------ store

class Store:
    """A live/draft/baseline settings store over three files.

    Paths are constructor arguments with no defaults on purpose: the kit does
    not know an add-on's `/data` layout, and tests must be able to point the
    whole thing at a tmpdir.

    `schema` is the add-on's validator. It is applied to a *candidate draft*
    on every write and again on the draft at apply time (a draft can be
    hand-edited on disk between the two, exactly as traefik re-validates
    before rendering). It may canonicalise: whatever it returns is what gets
    stored. If it raises, nothing is written.
    """

    def __init__(
        self,
        path_live: Path,
        path_draft: Path,
        path_baseline: Path,
        *,
        schema: Callable[[dict], dict] | None = None,
    ) -> None:
        self.path_live = Path(path_live)
        self.path_draft = Path(path_draft)
        self.path_baseline = Path(path_baseline)
        self._schema = schema
        # Aliasing any two of these would make one verb silently destroy
        # another's state; catch it at construction, not at 3am.
        paths = [self.path_live, self.path_draft, self.path_baseline]
        if len({str(p) for p in paths}) != 3:
            raise SettingsError(
                "path_live, path_draft and path_baseline must be three "
                f"distinct paths, got {[str(p) for p in paths]}"
            )
        if schema is not None and not callable(schema):
            raise SettingsError("schema must be callable or None")

    # -- reads ------------------------------------------------------------

    def live(self) -> dict:
        """What the service is running. {} when nothing has ever been applied."""
        current = _read(self.path_live)
        return {} if current is None else current

    def draft(self) -> dict:
        """What the user is editing.

        Falls back to live when the draft file has never been written, which
        is traefik's `_load_routes_draft` behaviour: an unseeded draft is not
        an empty config, it is "the same as live, untouched".
        """
        current = _read(self.path_draft)
        return self.live() if current is None else current

    def pending(self) -> dict:
        """What apply() would change. {} exactly when draft == live.

        Shape follows traefik's diff vocabulary:
        `{"added": {...}, "deleted": {...},
          "modified": {key: {"live": ..., "draft": ...}}}`.
        The comparison is over parsed values, so formatting differences on
        disk never show up as pending.
        """
        live = self.live()
        draft = self.draft()
        if live == draft:
            return {}
        return {
            "added": {k: v for k, v in draft.items() if k not in live},
            "deleted": {k: v for k, v in live.items() if k not in draft},
            "modified": {
                k: {"live": live[k], "draft": v}
                for k, v in draft.items()
                if k in live and live[k] != v
            },
        }

    def can_rollback(self) -> bool:
        """True only when an apply has actually changed live and has not yet
        been rolled back. A probe, so it never raises — an unreadable baseline
        reports False and logs at debug."""
        try:
            return _read(self.path_baseline) is not None
        except SettingsError as exc:
            _LOG.debug("baseline unreadable, reporting no rollback: %s", exc)
            return False

    # -- writes -----------------------------------------------------------

    def put_draft(self, patch: dict, *, merge: bool = True) -> dict:
        """Write the draft. Returns the stored draft.

        `merge=True` is a **shallow** top-level update — a deep merge would
        make deleting a key impossible to express. Use `merge=False` to
        replace the draft wholesale, which is also how you delete keys.
        Nothing here touches live; that is what apply() is for.
        """
        if not isinstance(patch, Mapping):
            raise SettingsError(
                f"patch must be a mapping, got {type(patch).__name__}"
            )
        candidate = {**self.draft(), **patch} if merge else dict(patch)
        candidate = self._validated(candidate)
        _atomic_write(self.path_draft, _dumps(candidate))
        return candidate

    def discard(self) -> dict:
        """draft <- live. Returns the new draft (== live).

        The schema is deliberately NOT run: live is by definition already
        valid, and a tightened schema must never be able to trap a user with
        an un-discardable draft.
        """
        live_bytes = _read_bytes_or_none(self.path_live)
        if live_bytes is None:
            # No live file: the correct draft is "unseeded", not "{}".
            self.path_draft.unlink(missing_ok=True)
            _fsync_dir(self.path_draft.parent)
        else:
            _atomic_write(self.path_draft, live_bytes)
        return self.live()

    def apply(self, on_apply: Callable[[dict], None]) -> dict:
        """live <- draft, baseline <- the previous live. Returns the new live.

        Order is the whole point, and it is the reason baseline is captured
        *before* rather than after:

          1. re-validate the draft (it may have been edited on disk);
          2. snapshot the current live and baseline bytes in memory;
          3. write baseline from the current live, then write live — both
             atomically;
          4. call `on_apply(new_live)`;
          5. if that raises, restore live AND baseline to the snapshots and
             raise SettingsError with the cause chained.

        So a failed apply leaves the running service on exactly the bytes it
        was already running, and leaves rollback armed at whatever it was
        armed at before.

        A process killed between the baseline write and the live write needs
        no journal the way traefik's three-file apply does: only one file
        moves, so the worst reachable state is a baseline that equals live,
        which makes a rollback a harmless no-op.

        Applying with nothing pending still calls `on_apply` — "reload with
        the current config" is a legitimate request and the kit does not
        decide policy — but it does NOT rewrite baseline. Rearming baseline
        to the current live on a no-op would silently disarm the rollback of
        the last real change.
        """
        if not callable(on_apply):
            raise SettingsError("on_apply must be callable")

        new_live = self._validated(self.draft())
        prev_live_bytes = _read_bytes_or_none(self.path_live)
        prev_baseline_bytes = _read_bytes_or_none(self.path_baseline)
        changed = self.live() != new_live

        if changed:
            # Baseline first: after this line live may be mid-flight, and the
            # last-known-good must already be on disk. A live that never
            # existed is faithfully recorded as {} — rolling back a first
            # apply returns you to unconfigured.
            _atomic_write(
                self.path_baseline,
                prev_live_bytes if prev_live_bytes is not None else _dumps({}),
            )
            _atomic_write(self.path_live, _dumps(new_live))

        try:
            on_apply(new_live)
        except BaseException as exc:
            if changed:
                try:
                    _restore(self.path_live, prev_live_bytes)
                    _restore(self.path_baseline, prev_baseline_bytes)
                except OSError as restore_exc:
                    raise SettingsError(
                        f"apply failed ({exc}) AND live could not be restored "
                        f"({restore_exc}) — {self.path_live} may be stale"
                    ) from exc
            if isinstance(exc, Exception):
                raise SettingsError(f"apply failed, live unchanged: {exc}") from exc
            raise

        # Only now normalise the draft, so a failed apply leaves the user's
        # draft byte-for-byte as they left it.
        _atomic_write(self.path_draft, _dumps(new_live))
        return new_live

    def rollback(self, on_apply: Callable[[dict], None] | None = None) -> dict:
        """live <- baseline. Returns the restored live.

        The draft is untouched and the baseline is consumed — see the module
        docstring for why both of those are the answer rather than an
        oversight.

        `on_apply` is optional and additive: the contract's signature is
        `rollback(self)`, and `store.rollback()` still behaves exactly as
        contracted. Pass the add-on's apply callback when you want the running
        service reloaded onto the restored config in the same call; if it
        raises, live is put back and the baseline stays armed, so a failed
        rollback is retryable.
        """
        baseline = _read(self.path_baseline)
        if baseline is None:
            raise SettingsError(
                "nothing to roll back: no baseline. A baseline exists only "
                "between an apply that changed live and the rollback that "
                "undoes it."
            )
        baseline_bytes = _read_bytes_or_none(self.path_baseline)
        assert baseline_bytes is not None  # existence just established
        prev_live_bytes = _read_bytes_or_none(self.path_live)

        _atomic_write(self.path_live, baseline_bytes)

        if on_apply is not None:
            try:
                on_apply(baseline)
            except BaseException as exc:
                _restore(self.path_live, prev_live_bytes)
                if isinstance(exc, Exception):
                    raise SettingsError(
                        f"rollback failed, live unchanged: {exc}"
                    ) from exc
                raise

        # Single depth: consume the baseline so a second rollback refuses
        # rather than flip-flopping. Only the next apply re-arms it.
        self.path_baseline.unlink(missing_ok=True)
        _fsync_dir(self.path_baseline.parent)
        return baseline

    # -- internals ---------------------------------------------------------

    def _validated(self, data: dict) -> dict:
        if self._schema is None:
            return data
        try:
            result = self._schema(data)
        except SettingsError:
            raise
        except Exception as exc:
            raise SettingsError(f"rejected by schema: {exc}") from exc
        if not isinstance(result, dict):
            raise SettingsError(
                f"schema must return a dict, returned {type(result).__name__}"
            )
        return result
