# ---------------------------------------------------------------------------
# VENDORED FILE -- DO NOT EDIT HERE.
# Source of truth: shared/addonkit/persist.py, in the private workspace repo.
# Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
# sync-shared.ps1 -Check reports it, and the next sync overwrites it.
# ---------------------------------------------------------------------------

"""Mirror an add-on's state between /data and /config so that uninstalling
WITHOUT ticking "delete configuration" behaves like a disable: reinstall and
everything is exactly as it was, with no setup step.

WHY THIS EXISTS
---------------
The supervisor ALWAYS deletes /data on uninstall. It is not a default and there
is no flag -- `App.unload()` calls `remove_data(self.path_data)`
unconditionally, and `uninstall()` always calls `unload()`. The checkbox in the
uninstall dialog is `remove_config`, and it governs a DIFFERENT directory:
`path_config`, which is the add-on's /config (the `addon_config` map). That one
survives unless the box is ticked, and `App.backup()` captures it alongside
/data -- so it is the only location that is both durable across an uninstall
and still inside add-on backups. /share would survive too and would silently
drop out of backups, which is a worse trap than the one this fixes.

So: /data stays the working directory, and /config holds a mirror.

  export()   on every clean service stop, including the graceful stop the
             supervisor performs immediately before it removes the container
             and deletes /data.
  restore()  from cont-init, ONLY when /data has no state at all.

The export runs from an s6 `finish` script, which the supervisor's stop gives
`timeout:` seconds to complete (10s by default, raisable to 300 in
config.yaml). A finish script must be present in the IMAGE: s6-overlay compiles
/etc/services.d/* into its s6-rc database at container init, so one dropped
into a running container is never registered.

SECURITY, stated plainly: the tracked files may carry secrets -- Traefik's
config.yml holds a Cloudflare API token and acme.json holds the ACME account
key. Mirroring them moves secrets from the add-on-private /data into /config,
which is user-visible (the Samba and File editor add-ons expose it). That is
the deliberate cost of "reinstall with nothing to re-enter". Files are written
0600 and the directory 0700; an add-on that cannot accept that must not list
the file.

This is a faithful port of ha-addon-traefik's rootfs state-sync.sh, which is
the shipped and working original.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Sequence

from .errors import KitError

_LOG = logging.getLogger(__name__)

# The container's real directories. Named constants rather than inline
# literals in the signature below because `main()` needs the same values for
# its --data-dir/--config-dir defaults, and two copies of "/data" is exactly
# how a path drifts. Constructing a Path is not I/O, so house rule 1 holds.
DEFAULT_DATA_DIR = Path("/data")
DEFAULT_CONFIG_DIR = Path("/config")

# Records when the last export ran. Also the gate on restore: no stamp means
# nothing was ever mirrored, so there is nothing to restore FROM.
STAMP_NAME = ".state-sync"

_FILE_MODE = 0o600
_DIR_MODE = 0o700

# Suffix for the write-then-rename staging file. Never a tracked name, so a
# leftover from a killed export is inert.
_TMP_SUFFIX = ".tmp"


class Mirror:
    """A fixed set of flat filenames mirrored between two directories.

    `files` are bare names, not paths: both the shell original and every
    consumer treat the add-on's state as a flat set, and allowing a
    subdirectory would mean reasoning about partially-created trees inside a
    10-second shutdown window.

    Paths are constructor arguments with the container values as defaults, so
    the whole class is exercisable against a tmpdir.
    """

    def __init__(
        self,
        files: Sequence[str],
        data_dir: Path = DEFAULT_DATA_DIR,
        config_dir: Path = DEFAULT_CONFIG_DIR,
    ) -> None:
        names: list[str] = []
        for raw in files:
            name = str(raw)
            # `Path(name).name` alone is not enough: on Linux it happily keeps
            # a backslash, and on Windows it strips one, so the two checks
            # together are what make the guard behave the same on both.
            if (
                not name
                or name in (".", "..")
                or "/" in name
                or "\\" in name
                or name != Path(name).name
            ):
                raise KitError(f"tracked file must be a bare filename, got {name!r}")
            if name == STAMP_NAME:
                raise KitError(f"{STAMP_NAME!r} is the mirror's own stamp file")
            if name.endswith(_TMP_SUFFIX):
                raise KitError(f"tracked file may not end in {_TMP_SUFFIX!r}: {name!r}")
            if name not in names:
                names.append(name)
        self.files: tuple[str, ...] = tuple(names)
        self.data_dir = Path(data_dir)
        self.config_dir = Path(config_dir)

    @property
    def stamp_path(self) -> Path:
        return self.config_dir / STAMP_NAME

    # ------------------------------------------------------------------

    def export(self) -> list[str]:
        """Copy every tracked file that exists in /data into /config.

        Returns the names actually mirrored. Best effort throughout: this runs
        while the container is being torn down, and a file we cannot read must
        cost us that one file, not the other eleven.
        """
        if not self.config_dir.is_dir():
            # INFO, not DEBUG: this runs from an s6 finish script during the
            # stop that precedes an uninstall. A quiet no-op here means the
            # user loses their configuration and nothing ever said so.
            _LOG.info(
                "%s is not a directory (addon_config not mapped?) -- nothing to "
                "mirror to", self.config_dir,
            )
            return []
        _chmod(self.config_dir, _DIR_MODE)

        done: list[str] = []
        for name in self.files:
            src = self.data_dir / name
            if not src.is_file():
                continue
            if _copy_atomic(src, self.config_dir / name):
                done.append(name)

        # Stamped even when nothing was copied, exactly as the shell does: the
        # stamp answers "did an export ever run", not "how much did it move".
        self._write_stamp()
        _LOG.info("mirrored %d file(s) to %s", len(done), self.config_dir)
        return done

    def restore(self) -> list[str]:
        """Copy the mirror back into /data -- but only into a /data that holds
        NONE of the tracked files.

        That condition is the whole safety property. A normal restart has live
        state in /data, and copying an older mirror over it would silently roll
        the user back to whenever the add-on last stopped. The only situation
        we want to act on is the one where the supervisor has just deleted
        /data: every tracked file gone at once.
        """
        if not self.stamp_path.is_file():
            _LOG.debug("no %s -- nothing was ever exported", self.stamp_path)
            return []

        live = [n for n in self.files if (self.data_dir / n).exists()]
        if live:
            # `.exists()`, not `.is_file()`: a directory or a dangling symlink
            # where a tracked file belongs is still evidence of live state, and
            # guessing otherwise is how a mirror overwrites something real.
            _LOG.info(
                "live state present in %s (%s) -- not restoring",
                self.data_dir, ", ".join(live[:3]),
            )
            return []

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            _LOG.debug("cannot create %s -- not restoring", self.data_dir, exc_info=True)
            return []

        done: list[str] = []
        for name in self.files:
            src = self.config_dir / name
            if not src.is_file():
                continue
            if _copy_atomic(src, self.data_dir / name):
                done.append(name)

        _LOG.info(
            "restored %d file(s) from %s (exported %s)",
            len(done), self.config_dir, self.stamp(),
        )
        return done

    def stamp(self) -> float | None:
        """Unix seconds of the last export, or None if there was never one or
        the stamp is unreadable. Never raises -- callers use it to phrase a
        message, not to decide anything load-bearing."""
        try:
            raw = self.stamp_path.read_text(encoding="ascii", errors="ignore").strip()
        except OSError:
            return None
        try:
            # Tolerates the shell original's integer `date +%s` as well as the
            # sub-second value written below.
            return float(raw)
        except ValueError:
            _LOG.debug("unparseable stamp in %s: %r", self.stamp_path, raw[:40])
            return None

    # ------------------------------------------------------------------

    def _write_stamp(self) -> None:
        tmp = self.config_dir / (STAMP_NAME + _TMP_SUFFIX)
        try:
            tmp.write_text(f"{time.time():.3f}\n", encoding="ascii")
            _chmod(tmp, _FILE_MODE)
            os.replace(tmp, self.stamp_path)
        except OSError:
            _LOG.debug("could not write %s", self.stamp_path, exc_info=True)
            _unlink_quiet(tmp)


# ---------------------------------------------------------------------------


def _copy_atomic(src: Path, dst: Path) -> bool:
    """Copy `src` over `dst` via a staging file in the destination directory.

    The rename is the point: a kill partway through the copy leaves a stray
    `.tmp` and a `dst` still holding the previous good contents, where a direct
    copy would leave a truncated file where a valid one used to be. `os.replace`
    is atomic within a filesystem, and both directories are single mounts.

    `copy2` mirrors the shell's `cp -p` so the mirror keeps the source mtime --
    useful for telling a stale mirror from a fresh one by hand. The mode it
    preserves is then overridden: 0600 is not negotiable in /config.
    """
    tmp = dst.with_name(dst.name + _TMP_SUFFIX)
    try:
        shutil.copy2(src, tmp)
        _chmod(tmp, _FILE_MODE)
        os.replace(tmp, dst)
        return True
    except (OSError, shutil.Error):
        _LOG.debug("could not mirror %s -> %s", src, dst, exc_info=True)
        _unlink_quiet(tmp)
        return False


def _chmod(path: Path, mode: int) -> None:
    """Best effort. Windows honours only the read-only bit, so this is close to
    a no-op on the workstation; failing here must never abort a mirror that
    otherwise worked."""
    try:
        os.chmod(path, mode)
    except OSError:
        _LOG.debug("could not chmod %s to %o", path, mode, exc_info=True)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """`python -m addonkit.persist export|restore` -- the shell-side seam.

    The mirror is driven from an s6 finish script and from cont-init, neither of
    which can import the web app. This is the spelling the contract promises;
    addonkit.app grows a richer CLI on top for add-ons that would rather declare
    their file list once, in Python.
    """
    import argparse
    import logging

    ap = argparse.ArgumentParser(prog="python -m addonkit.persist")
    ap.add_argument("action", choices=("export", "restore"))
    ap.add_argument("--files", required=True,
                    help="whitespace-separated tracked filenames")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="[state-sync] %(message)s")
    try:
        # Commas tolerated as well as whitespace: the shell original keeps its
        # FILES in a newline-separated variable, but a hand-written call is as
        # likely to comma-separate, and a stray "acme.json," would otherwise
        # fail the bare-filename guard with a traceback out of a finish script.
        mirror = Mirror(args.files.replace(",", " ").split(),
                        data_dir=Path(args.data_dir),
                        config_dir=Path(args.config_dir))
    except KitError as exc:
        ap.error(str(exc))          # exits 2
    touched = mirror.export() if args.action == "export" else mirror.restore()
    logging.getLogger(__name__).info(
        "%s: %d file(s)", args.action, len(touched)
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
