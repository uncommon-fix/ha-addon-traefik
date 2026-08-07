#!/usr/bin/env python3
"""Mirror this add-on's state between /data and /config.

The WHY -- that the supervisor always deletes /data on uninstall while /config
survives and is still captured by App.backup(), and that mirroring config.yml
and acme.json therefore moves secrets into a user-visible directory on purpose
-- is written out in full in addonkit/persist.py's module docstring. It is
deliberately NOT repeated here: this file is the file list and nothing else, so
there is only ever one copy of the reasoning to keep true. Where this runs in
the boot sequence is docs/boot-chain.md.

This replaces rootfs/usr/local/bin/state-sync.sh, which was the original of
addonkit.persist. It exists rather than the shell calling
`python3 -m addonkit.persist --files "..."` directly for one reason: the tracked
file list is declared ONCE, as importable Python, instead of a second time as a
shell variable that nothing checks against the first. `TRACKED_FILES` must stay
in step with the `/data` layout in docs/data-state-model.md.

Invoked as a script, exactly like the other backend entry points:

    python3 /usr/local/bin/backend/state_sync.py export     # s6 finish
    python3 /usr/local/bin/backend/state_sync.py restore    # cont-init

Running a script puts its own directory on sys.path, which is what makes the
bare `addonkit` import resolve with no PYTHONPATH and no -m.
"""

from __future__ import annotations

import logging
import sys

from addonkit.persist import main as persist_main

# Everything the add-on owns, in the order the shell original listed it.
# options.json is NOT here: the supervisor writes it from the add-on options
# and regenerates it on install.
#
# Do not trim this list without reading docs/data-state-model.md. The baselines
# are the 3-way-merge ancestors migrate.py needs on the next boot, and the
# journal is how a half-finished Apply is recovered -- restoring live and draft
# without them would resurrect state that then looks like unexplained drift.
TRACKED_FILES: tuple[str, ...] = (
    "routes.yml",
    "config.yml",
    "middlewares.yml",
    "routes.draft.yml",
    "config.draft.yml",
    "middlewares.draft.yml",
    ".routes.baseline.yml",
    ".config.baseline.yml",
    ".middlewares.baseline.yml",
    ".apply_journal.yml",
    ".draft_reset_reasons.json",
    "acme.json",
)


def main(argv: list[str] | None = None) -> int:
    """Delegate to the kit's CLI, supplying our file list.

    The kit's `main` owns the argument parsing and the export/restore
    semantics; all this adds is --files. It never raises out: a traceback from
    an s6 `finish` script makes s6 report the service as failed, and a failure
    from cont-init would abort a boot over a best-effort mirror.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return persist_main([*args, "--files", " ".join(TRACKED_FILES)])
    except SystemExit as exc:            # argparse: bad/missing action
        return int(exc.code or 0)
    except Exception:                    # noqa: BLE001 -- see docstring
        logging.getLogger(__name__).exception("state mirror failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
