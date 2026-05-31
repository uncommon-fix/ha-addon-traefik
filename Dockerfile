ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# Traefik v3.7.1 — latest stable 3.x at audit time; includes CVE-2026-44774 fix.
# SHAs from https://github.com/traefik/traefik/releases/download/v3.7.1/traefik_v3.7.1_checksums.txt
ARG TRAEFIK_VERSION=3.7.1
ARG TRAEFIK_SHA256_AMD64=e92bcfb03fa1e6a70c4e7ad4eb4f1604967e6fa3c21d8e7605aca5407a40162c
ARG TRAEFIK_SHA256_ARM64=911ad9f4c21a58fdcbf09c75d967a280c9eec22b3d056fc7f4950cd3294c22b8

# Phase D adds: py3-aiohttp for the backend UI server.
# One RUN to keep layer count down. apk (python + yaml + aiohttp + ca-certs;
# busybox already provides wget and tar) + sha256 verify + tar extract +
# chmod + cleanup + build-time smoke test that the binary runs.
# py3-aiohttp 3.9.3-r1 on Alpine 3.20 pulls in py3-aiosignal, py3-multidict,
# py3-yarl, py3-frozenlist as transitive deps automatically.
# alpha.6: py3-ruamel.yaml (Alpine community repo; auto-pulls py3-ruamel.yaml.clib)
# powers the comment-preserving configuration.yaml edit for the trusted_proxies
# quick-fix. NOTE the dot in the package name (py3-ruamel.yaml, not -yaml).
RUN apk add --no-cache python3 py3-yaml py3-aiohttp py3-bcrypt py3-ruamel.yaml ca-certificates \
 && case "$(uname -m)" in \
        x86_64) ARCH=amd64; SHA="${TRAEFIK_SHA256_AMD64}" ;; \
        aarch64) ARCH=arm64; SHA="${TRAEFIK_SHA256_ARM64}" ;; \
        *) echo "Unsupported arch: $(uname -m)" >&2; exit 1 ;; \
    esac \
 && wget -q "https://github.com/traefik/traefik/releases/download/v${TRAEFIK_VERSION}/traefik_v${TRAEFIK_VERSION}_linux_${ARCH}.tar.gz" -O /tmp/traefik.tar.gz \
 && echo "${SHA}  /tmp/traefik.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/traefik.tar.gz -C /usr/local/bin traefik \
 && chmod +x /usr/local/bin/traefik \
 && rm -f /tmp/traefik.tar.gz \
 && /usr/local/bin/traefik version \
 && python3 -c "import bcrypt; assert bcrypt.checkpw(b'x', bcrypt.hashpw(b'x', bcrypt.gensalt(4)))"

# Phase D — vendor Alpine.js at build time with SHA256 pinning into the image.
# CDN-at-build (NOT runtime), so the user's HA host doesn't need outbound
# internet for the UI to load.
#
# alpha.15: Tailwind 3.4.17 moved from CDN-at-build to in-repo (committed to
# `web/static/tailwindcss-3.4.17.min.js`, ~400 KB). Drops the network-at-build
# requirement entirely for Tailwind, eliminates the production-mode deprecation
# console.error logged on every page load when the Play CDN serves the
# minified bundle, and protects against the Play CDN going away (it's been in
# maintenance since Tailwind v4 shipped; 3.4.18+ already 200s with a console.error
# payload). Alpine.js's jsdelivr CDN is healthy, so it stays build-time-fetched
# for now. The `COPY web/` step below picks up the vendored Tailwind file.
ARG ALPINEJS_VERSION=3.15.12
ARG ALPINEJS_SHA256=57b37d7cae9a27d965fdae4adcc844245dfdc407e655aee85dcfff3a08036a3f

RUN mkdir -p /usr/share/traefik-web/static \
 && wget -q "https://cdn.jsdelivr.net/npm/alpinejs@${ALPINEJS_VERSION}/dist/cdn.min.js" \
        -O "/usr/share/traefik-web/static/alpinejs-${ALPINEJS_VERSION}.min.js" \
 && echo "${ALPINEJS_SHA256}  /usr/share/traefik-web/static/alpinejs-${ALPINEJS_VERSION}.min.js" | sha256sum -c -

# COPY order: static-est first (rootfs + renderer rarely change; backend + web
# iterate). This keeps the cache hot during Phase D execute / follow-up iter.

# s6-overlay v3 (HA base 3.23) accepts the legacy v2 layout under
# /etc/cont-init.d/ and /etc/services.d/ via its compat layer (mosquitto and
# every hassio-addons addon ships this shape). After COPY, explicitly chmod
# +x the run scripts -- Windows checkouts have no POSIX exec bits, and
# COPY --chmod doesn't compose with directory-tree COPY.
COPY rootfs/ /
RUN chmod +x /etc/services.d/backend/run \
             /etc/services.d/traefik/run \
             /etc/cont-init.d/00-prep.sh \
             /etc/cont-init.d/99-deploy-integration.sh

# render.py installed at the same path Phase B/C used; backend invokes it as a
# subprocess.
COPY --chmod=755 render.py /usr/local/bin/render.py

# Backend Python scripts -- flat layout under /usr/local/bin/backend/, invoked
# directly as `python3 /usr/local/bin/backend/server.py` (no -m, no PYTHONPATH
# gotchas). migrate.py runs once from cont-init.
COPY backend/ /usr/local/bin/backend/

# Static web assets the backend serves from /usr/share/traefik-web/{index.html,static/}.
# Re-COPY merges with the vendored JS from earlier RUN (same target dir).
COPY web/ /usr/share/traefik-web/

# Bundled reachability integration. cont-init's 99-deploy-integration.sh copies
# this tree to /homeassistant/custom_components/ on every boot when the version
# differs from what's already deployed. BUILD_VERSION is auto-injected by the
# home-assistant/builder CI action (= config.yaml's version:); the default
# below covers local builds — keep it in sync with config.yaml's version: field.
ARG BUILD_VERSION=0.1.0-alpha.21
# alpha.14: also export BUILD_VERSION as a runtime env var so the backend can
# read it for the app.js cache-buster query string. The integration's
# .bundled_version file already gives cont-init's deploy step a version to
# diff; this ENV gives server.py a value at import without a file read.
ENV ADDON_VERSION=${BUILD_VERSION}
COPY integration/custom_components /usr/share/traefik-integration/custom_components
RUN echo "${BUILD_VERSION}" > /usr/share/traefik-integration/.bundled_version

# No CMD: s6-overlay's `legacy-services` service runs CMD if present; with no
# CMD it becomes a silent no-op longrun. Our two real services are under
# /etc/services.d/{backend,traefik}/.
