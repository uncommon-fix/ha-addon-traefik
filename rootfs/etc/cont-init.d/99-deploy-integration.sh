#!/usr/bin/with-contenv bashio
# Deploy the bundled reachability integration to
# /homeassistant/custom_components/traefik/ if the baked-in version differs
# from what's on disk. Touch /data/integration_pending_restart so the
# backend's UI can prompt the user to restart HA Core.
#
# NO supervisor calls in this script — restart is user-triggered via the
# addon UI's banner. That avoids a cont-init/supervisor-startup deadlock.
set -euo pipefail

SRC=/usr/share/traefik-integration/custom_components/traefik
DST=/homeassistant/custom_components/traefik
VERSION_FILE_SRC=/usr/share/traefik-integration/.bundled_version
VERSION_FILE_DST="${DST}/.bundled_version"
MARKER=/data/integration_pending_restart

BUNDLED="$(cat "${VERSION_FILE_SRC}")"
DEPLOYED="$(cat "${VERSION_FILE_DST}" 2>/dev/null || echo none)"

if [ "${BUNDLED}" = "${DEPLOYED}" ]; then
    bashio::log.info "integration deploy: up-to-date (v${BUNDLED})"
    exit 0
fi

bashio::log.info "integration deploy: ${DEPLOYED} -> ${BUNDLED}"
mkdir -p "${DST}"
# busybox Alpine has no rsync; pre-clean + cp -a covers --delete semantics.
find "${DST}" -mindepth 1 -delete
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

touch "${MARKER}"
bashio::log.info "integration deployed (v${BUNDLED}); user must restart HA Core"
