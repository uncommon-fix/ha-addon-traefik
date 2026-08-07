#!/bin/sh
# Mirror this add-on's state between /data and /config so that uninstalling
# WITHOUT ticking "delete configuration" behaves like a disable: reinstall and
# everything is exactly as it was, with no setup step.
#
# WHY THIS EXISTS
# ---------------
# The supervisor ALWAYS deletes /data on uninstall. It is not a default and
# there is no flag -- apps/app.py App.unload() calls remove_data(self.path_data)
# unconditionally, and uninstall() always calls unload(). The checkbox in the
# uninstall dialog is `remove_config`, and it governs a DIFFERENT directory:
# path_config, which is this add-on's /config (the addon_config map). That one
# survives unless the box is ticked, and App.backup() captures it alongside
# /data, so it is the only location that is both durable across an uninstall
# and still inside add-on backups. /share would survive too and would silently
# drop out of backups, which is a worse trap than the one this fixes.
#
# So: /data stays the working directory, and /config holds a mirror.
#
#   export   on every clean service stop, including the graceful stop the
#            supervisor performs immediately before it removes the container
#            and deletes /data.
#   restore  from cont-init, only when /data has no state at all.
#
# The export runs from an s6 `finish` script, which the supervisor's stop gives
# `timeout:` seconds to complete (config.yaml raises it from the 10s default).
# Note that a finish script must be present in the IMAGE: s6-overlay compiles
# /etc/services.d/* into its s6-rc database at container init, so one dropped
# into a running container is never registered.
#
# SECURITY, stated plainly: config.yml carries the Cloudflare API token and
# acme.json carries the ACME account key and issued certificates. Mirroring
# them moves secrets from the add-on-private /data into /config, which is
# user-visible (the Samba and File editor add-ons expose it). That is the
# deliberate cost of "reinstall with nothing to re-enter". Both are written
# 0600 and the directory is 0700; see docs/state-persistence.md.

set -eu

DATA=/data
STATE=/config
STAMP="${STATE}/.state-sync"

# Everything the add-on owns. options.json is NOT here: the supervisor writes
# it from the add-on options and regenerates it on install.
FILES="
routes.yml
config.yml
middlewares.yml
routes.draft.yml
config.draft.yml
middlewares.draft.yml
.routes.baseline.yml
.config.baseline.yml
.middlewares.baseline.yml
.apply_journal.yml
.draft_reset_reasons.json
acme.json
"

log() { echo "[state-sync] $*"; }

do_export() {
    if [ ! -d "${STATE}" ]; then
        log "no ${STATE} (addon_config not mapped?) -- nothing to mirror to"
        return 0
    fi
    umask 077
    mkdir -p "${STATE}"
    chmod 700 "${STATE}" 2>/dev/null || true

    n=0
    for f in ${FILES}; do
        if [ -f "${DATA}/${f}" ]; then
            # Copy to a temp name and rename, so a kill midway through cannot
            # leave a truncated file where a good one used to be.
            cp -p "${DATA}/${f}" "${STATE}/${f}.tmp" 2>/dev/null || continue
            mv -f "${STATE}/${f}.tmp" "${STATE}/${f}"
            n=$((n + 1))
        fi
    done
    date +%s > "${STAMP}"
    log "mirrored ${n} file(s) to ${STATE}"
}

do_restore() {
    if [ ! -f "${STAMP}" ]; then
        return 0                      # nothing was ever exported
    fi
    # Only ever populate an EMPTY /data. A normal restart has intact state and
    # must not be clobbered by an older mirror; that would silently roll the
    # user back to whenever the add-on last stopped.
    for f in ${FILES}; do
        if [ -e "${DATA}/${f}" ]; then
            log "${DATA}/${f} exists -- live state present, not restoring"
            return 0
        fi
    done

    umask 077
    n=0
    for f in ${FILES}; do
        if [ -f "${STATE}/${f}" ]; then
            cp -p "${STATE}/${f}" "${DATA}/${f}"
            n=$((n + 1))
        fi
    done
    log "restored ${n} file(s) from ${STATE} (exported $(cat "${STAMP}" 2>/dev/null))"
}

case "${1:-}" in
    export)  do_export ;;
    restore) do_restore ;;
    *) echo "usage: $0 export|restore" >&2; exit 2 ;;
esac
