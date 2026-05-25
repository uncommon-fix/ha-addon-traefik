#!/usr/bin/with-contenv bashio
# Deploy the bundled reachability integration to
# /homeassistant/custom_components/traefik/ if the baked-in version differs
# from what's on disk, then write `.content_hash` — a deterministic hash of the
# deployed integration content. The integration snapshots that value when it
# loads (writing `.loaded_content_hash`); the add-on banner AND the HA Repairs
# card both show "restart required" when the two differ. Keying on CONTENT (not
# the version string) means add-on-only releases never falsely demand a restart.
#
# NO supervisor calls in this script — restart is user-triggered.
set -euo pipefail

SRC=/usr/share/traefik-integration/custom_components/traefik
DST=/homeassistant/custom_components/traefik
VERSION_FILE_SRC=/usr/share/traefik-integration/.bundled_version
VERSION_FILE_DST="${DST}/.bundled_version"

BUNDLED="$(cat "${VERSION_FILE_SRC}")"
DEPLOYED="$(cat "${VERSION_FILE_DST}" 2>/dev/null || echo none)"

if [ "${BUNDLED}" = "${DEPLOYED}" ]; then
    bashio::log.info "integration deploy: up-to-date (v${BUNDLED})"
    exit 0
fi

bashio::log.info "integration deploy: ${DEPLOYED} -> ${BUNDLED}"
mkdir -p "${DST}"
# busybox Alpine has no rsync; pre-clean + cp -a covers --delete semantics.
# Preserve .loaded_content_hash: the integration writes it on load to record
# which content HA imported. Wiping it here would make every redeploy (even an
# identical-content add-on-only release) look like "restart needed" until HA
# restarts and the integration rewrites it.
find "${DST}" -mindepth 1 ! -name '.loaded_content_hash' -delete
cp -a "${SRC}/." "${DST}/"
echo "${BUNDLED}" > "${VERSION_FILE_DST}"

# Write the addon's OWN resolvable hostname so the integration's config_flow
# defaults to the correct URL regardless of install source. A *local* add-on is
# reachable at `local-traefik`, but a *store/repo* install gets a different
# hostname (`<repo_slug>-traefik`) — hardcoding `local-traefik` would be wrong
# for every outside tester. Best-effort: if the lookup fails, drop the file and
# let the integration fall back to its built-in default (do not abort boot).
if ADDON_HOST="$(bashio::addon.hostname 2>/dev/null)" && [ -n "${ADDON_HOST}" ]; then
    echo "http://${ADDON_HOST}:8080" > "${DST}/.api_url"
    bashio::log.info "integration api_url hint: http://${ADDON_HOST}:8080"
else
    rm -f "${DST}/.api_url"
    bashio::log.warning "could not resolve addon hostname; integration will use its default api_url"
fi

# Deterministic content hash of the deployed integration (excludes the deploy
# marker dotfiles + __pycache__). Identical content -> identical hash, so an
# add-on-only release that re-copies the same files produces no "restart" signal.
CONTENT_HASH="$(cd "${DST}" && find . -type f \
    ! -name '.content_hash' ! -name '.loaded_content_hash' \
    ! -name '.api_url' ! -name '.bundled_version' \
    ! -path '*/__pycache__/*' \
    -exec sha256sum {} + | sort | sha256sum | cut -d' ' -f1)"
printf '%s' "${CONTENT_HASH}" > "${DST}/.content_hash"
bashio::log.info "integration deployed (v${BUNDLED}, content ${CONTENT_HASH:0:12}); restart HA Core to load it"
