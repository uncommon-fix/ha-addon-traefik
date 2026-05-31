// Alpine.js single-page state for the Traefik add-on Setup + Routes + Dashboard tabs.
// Backend contract:
//   GET  /api/config  -> { provider, cloudflare_token:"", acme_email, domain, state:{configured, missing, cloudflare_token_present} }
//   PUT  /api/config  -> { saved, state, restart_required }
//   GET  /api/state   -> { configured, missing, cloudflare_token_present }
//   GET  /api/routes  -> { domain, routes }
//   PUT  /api/routes  -> { saved, stderr }
//   GET  /api/status  -> Traefik's /api/overview (or 503 if Traefik down)

const ingressMeta = document.querySelector('meta[name="ingress-path"]');
const INGRESS_PATH = (ingressMeta && ingressMeta.content) || '';
// alpha.20: addon version is rendered into <meta name="app-version"> by
// serve_index. Sent as X-Addon-Version on every mutating call so the
// backend can 409 a stale tab after an addon upgrade.
const appVersionMeta = document.querySelector('meta[name="app-version"]');
const APP_VERSION = (appVersionMeta && appVersionMeta.content) || '';

// Phase F: stable per-route uid for the routes-table x-for :key. Without
// this, removeRoute() shifts indexes and Alpine tears down/recreates rows,
// losing focus + in-progress edits.
let _routeUid = 0;
let _mwUid = 0;

function makeBlankRoute() {
    return {
        _uid: ++_routeUid,
        hostname: '',
        backend_kind: 'home_assistant',
        backend_host: null,
        backend_port: null,
        scheme: 'http',
        tls: true,
        enabled: true,
        middlewares: [],
        // Phase E: per-route healthCheck.path override; null = renderer default ("/").
        health_path: null,
        // alpha.14: skip-TLS-verify on the backend (used to be a magic-string
        // middleware attachment; now an honest per-route bool). Drives the
        // service-level insecureSkipVerify transport in render.py.
        skip_tls_verify: false,
        // alpha.16: client-only UI state — controls inline expand/collapse of
        // the editor panel in the compact Routes view. Stripped by save()
        // (the wire payload only sends fields the server schema knows about).
        // Fresh routes start expanded so the user can edit the blank form;
        // existing routes load collapsed.
        _expanded: true,
        // makeBlankRoute does NOT set `system` -- user routes only.
        // The HA system route is seeded by migrate.py and persisted in routes.yml.
    };
}

// alpha.14: single source of truth for "this middleware is owned by a
// feature toggle, hide it from the relevant UI surface." Both filter sites
// (route dropdown + Middlewares tab list) consult this table. Adding a future
// feature-managed middleware = one entry here, no scattered edits.
// alpha.18: vendored MDI icon paths. We render proper SVGs instead of
// emoji glyphs (▶/▼/🔒) so the UI looks consistent and chip-sized
// markers don't push compact rows into wrap. MDI paths are open-source
// and visually match HA's own iconography. The addon UI runs inside an
// HA ingress iframe, so HA's <ha-icon> custom element is not reachable;
// reusing MDI's path data is the closest equivalent without a CDN dep.
const MDI_ICONS = {
    'chevron-down':  'M7.41,8.59L12,13.17L16.59,8.59L18,10L12,16L6,10L7.41,8.59Z',
    'chevron-right': 'M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z',
    'chevron-up':    'M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z',
    'lock':          'M12,17A2,2 0 0,0 14,15C14,13.89 13.1,13 12,13A2,2 0 0,0 10,15A2,2 0 0,0 12,17M18,8A2,2 0 0,1 20,10V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V10C4,8.89 4.9,8 6,8H7V6A5,5 0 0,1 12,1A5,5 0 0,1 17,6V8H18M12,3A3,3 0 0,0 9,6V8H15V6A3,3 0 0,0 12,3Z',
    'shield-alert':  'M12,1L3,5V11C3,16.55 6.84,21.74 12,23C17.16,21.74 21,16.55 21,11V5L12,1M11,7H13V13H11V7M11,15H13V17H11V15Z',
};
function mdiSvg(name, classes = '') {
    const path = MDI_ICONS[name];
    if (!path) return '';
    return `<svg viewBox="0 0 24 24" aria-hidden="true" class="${classes}"><path fill="currentColor" d="${path}"></path></svg>`;
}

const FEATURE_MANAGED_MIDDLEWARES = {
    'redirect-to-https': {
        // Hide from the Middlewares tab always: its two params
        // (scheme=https, permanent=true) are not user-knobs in practice;
        // migrate.py sets the canonical defaults.
        hideFromTab: () => true,
        // Hide from the per-route dropdown when force_ssl is on
        // (force_ssl globalises redirection at the entrypoint level).
        hideFromDropdown: (config) => !!(config && config.force_ssl),
    },
};

function normalizeRoutes(routes) {
    return (routes || []).map(r => Object.assign(makeBlankRoute(), r, {
        _uid: ++_routeUid,                 // fresh uid every load
        middlewares: Array.isArray(r.middlewares) ? r.middlewares : [],
        // Start collapsed on load (makeBlankRoute defaults to expanded for
        // fresh "Add route" rows).
        _expanded: false,
    }));
}

// Phase F: middleware-card factories. Each card has a stable _uid for x-for.
function emptyConfigFor(type) {
    if (type === 'basicAuth') return { users: [] };
    if (type === 'ipAllowList') return { sourceRange: [''] };
    if (type === 'redirectScheme') return { scheme: 'https', permanent: true };
    if (type === 'headers') return { customRequestHeaders: [], customResponseHeaders: [] };
    return {};
}

function makeBlankMiddleware() {
    return {
        _uid: ++_mwUid,
        name: '',
        type: 'basicAuth',
        config: emptyConfigFor('basicAuth'),
    };
}

// UI shape: headers customRequestHeaders/customResponseHeaders are edited as
// [{key, value}] rows so blank in-progress rows and insertion order are
// preserved. They round-trip to the wire as {key: value} dicts.
function dictToRows(d) {
    return Object.entries(d || {}).map(([key, value]) => ({key, value}));
}
function rowsToDict(rows) {
    const out = {};
    for (const r of rows || []) {
        const k = ((r && r.key) || '').trim();
        if (!k) continue;
        if (k in out) console.warn(`duplicate header key ${k}; last value wins`);
        out[k] = (r && r.value) || '';
    }
    return out;
}

function normalizeMiddlewares(defs) {
    return (defs || []).map(m => {
        const type = m.type;
        let cfg = m.config || {};
        // For basicAuth, each user gets a local password input + _orig_username.
        if (type === 'basicAuth') {
            cfg = {
                users: (cfg.users || []).map(u => ({
                    username: u.username || '',
                    password: '',
                    password_set: !!u.password_set,
                    _orig_username: u.username || '',
                })),
            };
        } else if (type === 'headers') {
            cfg = {
                customRequestHeaders: dictToRows(cfg.customRequestHeaders),
                customResponseHeaders: dictToRows(cfg.customResponseHeaders),
            };
        } else if (type === 'ipAllowList') {
            cfg = { sourceRange: (cfg.sourceRange || []).slice() };
        }
        // redirectScheme: shape is already flat {scheme, permanent}.
        // alpha.14: skipTlsVerify no longer exists as a middleware type.
        // system flag (add-on built-in) is server-derived; the UI uses it to
        // gray/lock the card.
        return { _uid: ++_mwUid, name: m.name, type, config: cfg, system: !!m.system };
    });
}

function blankConfig() {
    return {
        provider: 'cloudflare',
        cloudflare_token: '',
        acme_email: '',
        domain: '',
        // Phase F: ha_hostname removed -- the HA system route owns the
        // subdomain now (pinned row in the Routes tab).
        entrypoint_http: 'web',
        entrypoint_https: 'websecure',
        log_level: 'INFO',
        force_ssl: false,
    };
}

function blankState() {
    return {
        configured: false,
        missing: [],
        cloudflare_token_present: false,
        // alpha.6: configuration.yaml lacks the trusted_proxies/use_x_forwarded_for
        // config (HTTPS-through-Traefik 400s without it).
        trusted_proxies_pending: false,
        // Integration banner (content-hash-derived). update_pending = was loaded,
        // new content deployed (State A); available = deployed but never added
        // (State B). Both default false so a poll error hides the banners.
        integration_pending_restart: false,
        integration_available: false,
    };
}

function traefikAppData() {
    return {
        // alpha.18: MDI icon helper exposed to Alpine templates via
        // `x-html="icon('chevron-down', 'w-4 h-4 …')"`. Path data lives
        // in module-level MDI_ICONS so it isn't duplicated per instance.
        icon(name, classes = '') { return mdiSvg(name, classes); },

        ingressPath: INGRESS_PATH,
        appVersion: APP_VERSION,
        // Default 'routes' once configured; load() flips to 'config'
        // (with wizardOpen=true) on first run so a sensible underlay
        // sits behind the wizard overlay.
        tab: 'routes',
        dashboardLoaded: false,

        // alpha.12: session takeover. The backend allows ONE active editor at
        // a time. On load() the UI POSTs /api/session/claim; a 409 surfaces a
        // takeover prompt (or "View read-only" → viewMode='ro' disables every
        // mutation). The SID is included as X-Session-Id on every mutating
        // request; a 423 from the backend means our session was taken over
        // from elsewhere — the UI shows a sticky toast with a Reload button.
        sid: '',
        viewMode: 'rw',                  // 'rw' = primary editor; 'ro' = read-only observer
        takeoverPrompt: { visible: false, age: 0 },
        sessionLost: false,              // true after a 423 from any mutation

        // alpha.12: unified toast/notification queue. Success/info auto-fade
        // after 4s; errors stick until the user dismisses them. Migrated from
        // the six ad-hoc *Error / four *Ok state slots that used to live here.
        toasts: [],
        _toastUid: 0,

        // Configuration tab + wizard overlay share the same config object.
        // wizardOpen is shown on first-load when !configured, or when the
        // user clicks "Re-run setup wizard" on the Configuration page.
        config: blankConfig(),
        state: blankState(),
        wizardOpen: false,
        savingConfig: false,
        restartRequired: false,
        restarting: false,

        // Phase 4 (0.9.0): HA Core restart triggered from the integration-
        // deploy banner. Distinct state from the addon-self-restart above so
        // both flows can run independently without UI confusion.
        restartingCore: false,

        // alpha.6: trusted_proxies quick-fix banner.
        fixingTrustedProxies: false,
        trustedProxiesFixed: false,
        showTpSnippet: false,
        // alpha.6: dismiss the "integration available" (State B) banner.
        dismissingIntegration: false,

        // alpha.12: per-section load failure marker. When a loadX() fails, its
        // key is set to the error message; the section renders a "Couldn't
        // load — Reload to retry" panel and its Save button is disabled. This
        // kills the C1 data-loss bug: a load failure can't slip into the save
        // error slot and trick the user into clicking Save and clobbering the
        // real config with an empty payload.
        loadFailed: { config: '', routes: '', middlewares: '' },

        // Routes tab
        routes: [],
        saving: false,
        // alpha.20: live caches for per-row diff + soft-delete restore. Set
        // by loadRoutes/loadMiddlewares/loadConfig (always fetched alongside
        // the draft view). Refreshed after every successful Apply.
        routesLive: [],
        middlewaresLive: [],
        configLive: {},
        // alpha.20: pending-changes summary from GET /api/pending. Refreshed
        // after every successful auto-save flush + on load. Drives the
        // sticky Apply footer at the bottom of the page.
        pending: { routes: { modified: [], added: [], deleted: [] },
                   middlewares: { modified: [], added: [], deleted: [] },
                   config: { modified: [] },
                   warnings: [], total: 0 },
        // alpha.20: auto-save bookkeeping. Per-surface debounce timer +
        // in-flight marker. _suspendAutoSave skips watcher firings during
        // the initial load() so we don't immediately re-PUT what we just
        // GET'd. autoSaveError tracks the last failure per surface so the
        // UI can show "auto-save failing" without spamming toasts.
        _autoSaveTimer: { routes: null, middlewares: null, config: null },
        _autoSaveInflight: { routes: false, middlewares: false, config: false },
        _suspendAutoSave: true,                       // true until first load completes
        autoSaveError: { routes: '', middlewares: '', config: '' },
        // alpha.20: Apply + Discard state.
        applying: false,
        discarding: false,
        discardConfirmOpen: false,
        // alpha.16: Routes-tab grouping state. groupBy: 'externalTarget' |
        // 'none'. collapsedGroups: Set of currently-collapsed group keys
        // (interned with the same key shape groupedRoutes emits). Both load
        // from localStorage in load() via _loadUiPref; setters call
        // _saveUiPref so the user's view sticks across reloads.
        // alpha.19: tags option removed; only externalTarget / none remain.
        groupBy: 'externalTarget',
        collapsedGroups: new Set(),
        // alpha.19: column sort. Click a sortable column header → cycle
        // asc → desc → clear. When clear (sortKey === ''), routes fall back
        // to alphabetical-by-hostname within their group (the alpha.16
        // default). Sort applies within each group when grouped, across the
        // flat list when not — the system route stays pinned to the top of
        // its group regardless (the alpha.17 fix that drove the explicit
        // pin in the first place). Persisted to localStorage so the user's
        // sort sticks across reloads + across groupby toggles.
        sortKey: '',          // '' | 'hostname' | 'backend' | 'scheme' | 'enabled' | 'status'
        sortDir: 'asc',       // 'asc' | 'desc'

        // Phase F: Middlewares tab
        middlewares: [],
        savingMw: false,

        // Dashboard / status badge
        status: {},
        _statusTimer: null,
        // alpha.10: per-route backend reachability (hostname -> up|down|unknown|
        // disabled), refreshed by pollStatus; drives the status dot per route.
        routeHealth: {},
        // alpha.12: track consecutive backend poll failures so the UI can
        // surface a sticky "backend unreachable" toast after 3 in a row (15s)
        // and stop the status dots from lying green when /api/route-health
        // can't actually be reached.
        _pollFailCount: 0,
        _pollFailToastId: 0,

        // alpha.12: client-side validators mirroring the server (kept in one
        // block so they stay trivially in sync with backend regexes). Each
        // getter returns an error message string or '' when valid; bound from
        // the template via x-text and used to gate Save buttons.
        get cloudflareTokenError() {
            const t = (this.config.cloudflare_token || '').trim();
            // Empty = "keep existing" (backend honors this); not an error.
            if (!t) return '';
            return /^[A-Za-z0-9_-]{20,256}$/.test(t)
                ? ''
                : 'Must be 20–256 chars from A–Z, a–z, 0–9, _, - (no whitespace or newlines).';
        },
        get acmeEmailError() {
            const e = (this.config.acme_email || '').trim();
            if (!e) return 'Required.';
            return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e)
                ? ''
                : 'Looks like an invalid email address.';
        },
        get domainError() {
            const d = (this.config.domain || '').trim().toLowerCase();
            if (!d) return 'Required.';
            return /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/.test(d)
                ? ''
                : 'Looks like an invalid domain (use lowercase a–z, 0–9, -, and dots).';
        },
        _entrypointError(value) {
            const v = (value || '').trim().toLowerCase();
            if (!v) return 'Required.';
            if (v === 'traefik') return '"traefik" is reserved.';
            return /^[a-z][a-z0-9_-]{0,30}$/.test(v)
                ? ''
                : 'Must start with a–z, then up to 30 of a–z, 0–9, _, -.';
        },
        get entrypointHttpError() { return this._entrypointError(this.config.entrypoint_http); },
        get entrypointHttpsError() { return this._entrypointError(this.config.entrypoint_https); },
        get entrypointConflictError() {
            return (this.config.entrypoint_http || '').trim().toLowerCase() ===
                   (this.config.entrypoint_https || '').trim().toLowerCase()
                ? 'HTTP and HTTPS entry-point names must differ.' : '';
        },
        // Aggregated guards — Configuration save and Wizard save use distinct
        // field sets, so each has its own valid getter.
        get configFormValid() {
            return !this.entrypointHttpError && !this.entrypointHttpsError &&
                   !this.entrypointConflictError && !this.cloudflareTokenError;
        },
        get wizardFormValid() {
            // Wizard doesn't expose entry points or log level; just the four
            // initial-config fields. Token may be blank only if one is already
            // stored (preserve-existing) — backend tolerates blank on PUT.
            const tokenOk = !this.cloudflareTokenError &&
                            (this.config.cloudflare_token.trim() ||
                             this.state.cloudflare_token_present);
            return tokenOk && !this.acmeEmailError && !this.domainError;
        },
        // Auto-lowercase entry-point names on blur so the typed value matches
        // what the server normalises to (avoids "I typed Web, why does it now
        // say web?" confusion).
        normalizeEntrypoint(field) {
            const v = (this.config[field] || '').trim().toLowerCase();
            if (this.config[field] !== v) this.config[field] = v;
        },

        // alpha.12: composite list of "HA Core restart needed" reasons,
        // collapsing the previously-stacked three amber/sky banners into one
        // banner with bullet points. Each reason carries a stable `key` (used
        // as the x-for :key) and an optional `dismissable` flag for the
        // "integration available" case which is a soft suggestion.
        get coreRestartReasons() {
            const reasons = [];
            if (this.state.integration_pending_restart) {
                reasons.push({
                    key: 'integration-update',
                    text: 'Reachability integration was updated.',
                });
            }
            if (this.trustedProxiesFixed) {
                reasons.push({
                    key: 'trusted-proxies',
                    text: 'configuration.yaml was updated (trusted_proxies).',
                });
            }
            if (this.state.integration_available) {
                reasons.push({
                    key: 'integration-available',
                    text: 'Reachability sensors are available to add (optional).',
                    dismissable: true,
                });
            }
            return reasons;
        },
        get coreRestartNeeded() {
            return this.coreRestartReasons.length > 0;
        },

        // alpha.16: Routes-tab grouping. Returns
        //   [{key, label, count, routes: [...]}, ...]
        // The template iterates this; each group renders a clickable header
        // (collapse toggle) followed by its routes as compact rows.
        //
        // groupBy values (alpha.19: tag grouping removed):
        //   - 'externalTarget' (default): group by route.backend_host for
        //     external; "Home Assistant" for home_assistant kind. Port is
        //     ignored on purpose so e.g. 10.0.0.20:443 and 10.0.0.20:8006
        //     share a group.
        //   - 'none': single flat group containing everything (keeps the
        //     template shape uniform regardless of grouping choice).
        //
        // Group order: "Home Assistant" pinned to top (system route is
        // always visible), then alphabetical by label.
        // Within a group: _sortRoutes applies the active column sort
        // (system row always pinned to top of its group).
        get groupedRoutes() {
            const groups = new Map();      // key -> {key, label, count, routes}
            const ensure = (key, label) => {
                if (!groups.has(key)) {
                    groups.set(key, { key, label, count: 0, routes: [] });
                }
                return groups.get(key);
            };
            const keyFor = (r) => {
                if (this.groupBy === 'none') {
                    return { key: 'all', label: 'All routes' };
                }
                if (this.groupBy === 'externalTarget') {
                    if (r.backend_kind === 'external') {
                        const host = (r.backend_host || '').trim();
                        // alpha.17: nicer empty-host label (was "(no host)
                        // (1)", terse and ugly). The key stays distinct so
                        // multiple empty-host routes still co-group.
                        if (!host) {
                            return { key: 'ext:_empty',
                                     label: 'External backend (no host set)' };
                        }
                        return { key: 'ext:' + host, label: host };
                    }
                    return { key: 'ha', label: 'Home Assistant' };
                }
                // Defensive: unknown groupBy → flat list.
                return { key: 'all', label: 'All routes' };
            };
            // alpha.20: include orphan rows (routes in live but missing
            // from draft = user clicked Remove, not yet Applied) so they
            // render with strikethrough + Restore button inline with their
            // group. _sortRoutes pushes orphans to the bottom of the group.
            const allRows = [...this.routes, ...this._orphanRoutes];
            for (const r of allRows) {
                const { key, label } = keyFor(r);
                const g = ensure(key, label);
                g.routes.push(r);
                g.count++;
            }
            for (const g of groups.values()) {
                g.routes = this._sortRoutes(g.routes);
            }
            // Order the groups: HA pinned to top, then alphabetical by label.
            return [...groups.values()].sort((a, b) => {
                if (a.key === 'ha') return -1;
                if (b.key === 'ha') return 1;
                return a.label.localeCompare(b.label);
            });
        },

        // alpha.19: sort a routes array per the active sortKey/sortDir, with
        // the system row pinned to the top regardless (alpha.17 invariant —
        // the HA self-route must stay anchored so users don't lose track of
        // it). When sortKey is '' the fallback is alphabetical-by-hostname,
        // matching the alpha.16-through-alpha.18 default.
        _sortRoutes(routes) {
            const key = this.sortKey;
            const dir = this.sortDir === 'desc' ? -1 : 1;
            const valueOf = (r) => {
                switch (key) {
                    case 'hostname': return (r.hostname || '').toLowerCase();
                    case 'backend':  return this.compactBackendLabel(r).toLowerCase();
                    case 'scheme':   return (r.scheme || '').toLowerCase();
                    case 'enabled':  return r.enabled ? 0 : 1;   // On first when asc
                    case 'status': {
                        // Order: up < unknown < down < disabled. Surfaces
                        // problems near the top when sorted desc.
                        const rank = { up: 0, unknown: 1, down: 2, disabled: 3 };
                        const s = this.routeHealth[r.hostname] || 'unknown';
                        return rank[s] ?? 4;
                    }
                    default: return (r.hostname || '').toLowerCase();
                }
            };
            return [...routes].sort((a, b) => {
                // alpha.17: system row pinned to top regardless of sort.
                if (a.system && !b.system) return -1;
                if (!a.system && b.system) return 1;
                // alpha.20: soft-deleted (orphan) rows pinned to BOTTOM of
                // their group regardless of sort, so the active list stays
                // scannable. The audit specifically flagged this.
                if (a._orphan && !b._orphan) return 1;
                if (!a._orphan && b._orphan) return -1;
                const av = valueOf(a), bv = valueOf(b);
                if (av < bv) return -1 * dir;
                if (av > bv) return  1 * dir;
                // Stable tiebreak by hostname so a clear column doesn't
                // shuffle rows on every render.
                return (a.hostname || '').localeCompare(b.hostname || '');
            });
        },

        // alpha.19: click a column header to cycle its sort
        // (asc → desc → clear). Persists to localStorage. UI uses
        // sortIndicator(key) to draw the active arrow.
        toggleSort(key) {
            if (this.sortKey !== key) {
                this.sortKey = key;
                this.sortDir = 'asc';
            } else if (this.sortDir === 'asc') {
                this.sortDir = 'desc';
            } else {
                this.sortKey = '';
                this.sortDir = 'asc';
            }
            this._saveUiPref('traefik-addon:routes-sort',
                JSON.stringify({ key: this.sortKey, dir: this.sortDir }));
        },
        sortIndicator(key) {
            if (this.sortKey !== key) return '';
            return this.sortDir === 'desc' ? 'chevron-down' : 'chevron-up';
        },

        // alpha.16: flat list the Routes table template iterates over.
        //   - {type: 'header', _key, key, label, count}
        //   - {type: 'route',  _key, r}
        // One <tbody> per item in the rendered table — that's the only
        // shape Alpine's x-for permits when we need to emit two <tr>s per
        // route (compact + expanded). Collapsed groups emit just their
        // header item; their routes are absent from the list entirely.
        get routesTableItems() {
            const items = [];
            for (const grp of this.groupedRoutes) {
                items.push({
                    type: 'header',
                    _key: 'h:' + grp.key,
                    key: grp.key,
                    label: grp.label,
                    count: grp.count,
                });
                if (this.isGroupCollapsed(grp.key)) continue;
                for (const r of grp.routes) {
                    items.push({ type: 'route', _key: 'r:' + r._uid, r });
                }
            }
            return items;
        },

        // alpha.16: backend summary text for the compact row. Centralised so
        // the cell + the screen-reader label can share one source of truth.
        compactBackendLabel(r) {
            if (r.backend_kind === 'home_assistant') return 'Home Assistant';
            const host = r.backend_host || '';
            const port = r.backend_port ? (':' + r.backend_port) : '';
            return host || port ? (host + port) : '(unset)';
        },

        // alpha.16: dropdown options for the "Group by" selector.
        // alpha.19: tag options removed; static two-entry list.
        get groupByOptions() {
            return [
                { value: 'externalTarget', label: 'External target (default)' },
                { value: 'none', label: 'None (flat list)' },
            ];
        },

        // alpha.16: group + route ui-state helpers. Each is a no-op when the
        // input doesn't match (defensive; the template binds these to user
        // clicks but a stale ref or race could miss). _saveUiPref persists.
        isGroupCollapsed(key) {
            return this.collapsedGroups.has(key);
        },
        toggleGroupCollapsed(key) {
            if (this.collapsedGroups.has(key)) this.collapsedGroups.delete(key);
            else this.collapsedGroups.add(key);
            this._saveUiPref(
                'traefik-addon:routes-collapsed',
                JSON.stringify([...this.collapsedGroups])
            );
        },
        setGroupBy(value) {
            this.groupBy = value || 'externalTarget';
            this._saveUiPref('traefik-addon:routes-groupby', this.groupBy);
        },
        toggleRouteExpanded(r) {
            r._expanded = !r._expanded;
        },

        // alpha.16: localStorage helpers. No prior usage in this codebase —
        // this establishes the pattern. Both wrap try/catch so a private-mode
        // browser (storage disabled) silently falls back to in-memory state
        // without console noise. Keys are namespaced under 'traefik-addon:'.
        _loadUiPref(key, fallback) {
            try {
                const v = window.localStorage.getItem(key);
                return v === null ? fallback : v;
            } catch (_) {
                return fallback;
            }
        },
        _saveUiPref(key, value) {
            try {
                window.localStorage.setItem(key, value);
            } catch (_) { /* private mode / quota — drop quietly */ }
        },

        // Phase F: block Save when an enabled system route has empty hostname
        // (renderer would skip it, making HA unreachable through Traefik).
        get systemRowInvalid() {
            return this.routes.some(
                r => r.system && r.enabled && !(r.hostname || '').trim()
            );
        },

        // ---------- alpha.12: toast queue ----------
        // Lightweight notification API. Success/info auto-fade after `ttl` ms;
        // errors are sticky by default (caller can override). Each toast has a
        // stable _uid for the x-for :key so the DOM survives reorderings.
        _pushToast({ kind, text, sticky = false, ttl = 4000 }) {
            const id = ++this._toastUid;
            const toast = { id, kind, text, sticky };
            this.toasts.push(toast);
            if (!sticky) {
                setTimeout(() => this._dismissToast(id), ttl);
            }
            return id;
        },
        _dismissToast(id) {
            const i = this.toasts.findIndex(t => t.id === id);
            if (i >= 0) this.toasts.splice(i, 1);
        },
        get toast() {
            return {
                success: (text, opts = {}) => this._pushToast({ kind: 'success', text, ...opts }),
                info:    (text, opts = {}) => this._pushToast({ kind: 'info',    text, ...opts }),
                // Errors stick by default; the user explicitly dismisses.
                error:   (text, opts = {}) => this._pushToast({ kind: 'error',   text, sticky: true, ...opts }),
                dismiss: (id) => this._dismissToast(id),
            };
        },

        // ---------- alpha.12: fetch wrapper ----------
        // Auto-injects X-Session-Id on mutations so the backend's
        // session_gate_mw doesn't 423 our own saves. Raises a tagged
        // SessionLost-class Error on 423 so the caller can switch to the
        // takeover toast instead of a generic save-failed message.
        async api(method, path, body) {
            const headers = {};
            if (body !== undefined) headers['Content-Type'] = 'application/json';
            if (this.sid && method !== 'GET') headers['X-Session-Id'] = this.sid;
            // alpha.20: every mutating call sends X-Addon-Version so the
            // backend can 409 a stale browser tab after an addon upgrade
            // (avoids sending an old payload shape to a new validator).
            // {{APP_VERSION}} is substituted by server.serve_index; if the
            // literal placeholder is still there (unexpected) we just send
            // empty, which the backend ignores.
            if (method !== 'GET' && this.appVersion) {
                headers['X-Addon-Version'] = this.appVersion;
            }
            const r = await fetch(this.url(path), {
                method,
                headers,
                body: body !== undefined ? JSON.stringify(body) : undefined,
            });
            if (r.status === 423) {
                const err = new Error('Your session was taken over from another tab or browser.');
                err.code = 'SESSION_LOST';
                this.onSessionLost();
                throw err;
            }
            if (r.status === 409) {
                // alpha.20: VERSION_MISMATCH gets a distinct code so the
                // caller can prompt a reload (other 409s — like Apply no-op
                // — surface as normal errors).
                const j = await r.json().catch(() => ({}));
                if (j.code === 'VERSION_MISMATCH') {
                    const err = new Error(j.error || 'Addon version mismatch — reload required.');
                    err.code = 'VERSION_MISMATCH';
                    throw err;
                }
                // Re-emit as a regular error so the caller's catch sees it.
                throw new Error(j.error || `HTTP 409`);
            }
            if (!r.ok) {
                // alpha.11+ backend returns {"error": "..."} JSON for every error;
                // fall back to the bare status if for some reason the body isn't JSON.
                const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
                throw new Error(j.error || `${method} ${path} -> ${r.status}`);
            }
            // 204 No Content would lack a body; aiohttp endpoints here always
            // return JSON on success, but guard for future-proofing.
            const ct = r.headers.get('Content-Type') || '';
            if (ct.includes('application/json')) return await r.json();
            return null;
        },

        // ---------- alpha.12: session ----------
        async claimSession() {
            // Best-effort: a failure here lets the user see "backend unreachable"
            // via the toast / polling indicator rather than a hard error before
            // anything else loads.
            // alpha.15: include X-Session-Id when we already have one so the
            // server can short-circuit a re-claim to success (same browser,
            // same session). Without this, any code path that re-calls
            // claimSession would 409 against its own existing session.
            try {
                const headers = this.sid ? { 'X-Session-Id': this.sid } : {};
                const r = await fetch(this.url('/api/session/claim'), {
                    method: 'POST', headers,
                });
                if (r.status === 409) {
                    const j = await r.json().catch(() => ({}));
                    this.takeoverPrompt = {
                        visible: true,
                        age: Math.max(0, Math.round(j.current_age_s || 0)),
                    };
                    return false;
                }
                if (!r.ok) {
                    this.toast.error(`Couldn't claim editor session: HTTP ${r.status}. ` +
                                     `Some saves may be rejected.`);
                    return false;
                }
                const j = await r.json();
                this.sid = j.sid || '';
                this.viewMode = 'rw';
                return true;
            } catch (e) {
                this.toast.error(`Couldn't reach add-on backend: ${e.message}`);
                return false;
            }
        },
        async takeover() {
            // Force-become the active editor. The previous session's next
            // mutation will 423 → its UI shows the SessionLost toast.
            try {
                const r = await fetch(this.url('/api/session/takeover'), { method: 'POST' });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                const j = await r.json();
                this.sid = j.sid || '';
                this.viewMode = 'rw';
                this.takeoverPrompt = { visible: false, age: 0 };
                this.sessionLost = false;
                this.toast.success('Session taken over; you are now the active editor.');
                // Reload data with the new SID in case the prior editor saved
                // mid-flight before we took over.
                await this.loadConfig();
                await this.loadRoutes();
                await this.loadMiddlewares();
            } catch (e) {
                this.toast.error(`Take-over failed: ${e.message}`);
            }
        },
        viewReadOnly() {
            this.viewMode = 'ro';
            this.takeoverPrompt = { visible: false, age: 0 };
            this.toast.info('Read-only mode: another session is editing. Take over to make changes.');
        },
        onSessionLost() {
            // Idempotent: only push the sticky toast once per session-loss event.
            if (this.sessionLost) return;
            this.sessionLost = true;
            this.sid = '';
            this.viewMode = 'ro';
            this.toast.error(
                'Your session was taken over from another tab or browser. ' +
                'Reload to continue editing.',
                { sticky: true }
            );
        },

        async load() {
            // alpha.16: hydrate Routes-tab UI preferences from localStorage
            // BEFORE the first render so the user's view sticks across
            // reloads. groupBy falls back to the default; collapsedGroups
            // tolerates a malformed JSON string (treat as empty set).
            // alpha.19: alongside groupby/collapsed, hydrate sortKey + sortDir
            // from the same pref bucket so the user's last sort sticks too.
            this.groupBy = this._loadUiPref(
                'traefik-addon:routes-groupby', 'externalTarget'
            );
            try {
                const raw = this._loadUiPref(
                    'traefik-addon:routes-collapsed', '[]'
                );
                const arr = JSON.parse(raw);
                this.collapsedGroups = new Set(
                    Array.isArray(arr) ? arr : []
                );
            } catch (_) {
                this.collapsedGroups = new Set();
            }
            try {
                const raw = this._loadUiPref('traefik-addon:routes-sort', '');
                if (raw) {
                    const s = JSON.parse(raw);
                    if (s && typeof s === 'object') {
                        const ALLOWED_KEYS = new Set([
                            '', 'hostname', 'backend', 'scheme', 'enabled', 'status',
                        ]);
                        if (ALLOWED_KEYS.has(s.key)) this.sortKey = s.key;
                        if (s.dir === 'asc' || s.dir === 'desc') this.sortDir = s.dir;
                    }
                }
            } catch (_) { /* malformed — keep defaults */ }

            // alpha.12: claim the editor session FIRST. If 409, the takeover
            // modal appears; we still poll status so the user sees Traefik
            // state, and we still load read data so the read-only view is
            // populated.
            await this.claimSession();
            // Always load config first so we can decide what to show.
            await this.loadConfig();
            // First-run UX: not configured -> open wizard. Underneath, put
            // the Configuration tab (the Routes tab is gated to disabled
            // while unconfigured, so 'routes' would render as a no-op).
            if (!this.state.configured) {
                this.tab = 'config';
                this.wizardOpen = true;
            }
            await this.loadRoutes();
            await this.loadMiddlewares();   // Phase F
            // alpha.20: All surfaces are loaded; re-enable auto-save so the
            // Alpine watchers can fire on actual user edits. The watchers
            // are registered in alpine:init (bottom of file).
            // Defer one tick so any pending Alpine reactivity from the
            // loaders settles BEFORE we unblock the watchers.
            await new Promise(r => setTimeout(r, 0));
            this._suspendAutoSave = false;
            this.pollStatus();
            if (!this._statusTimer) {
                this._statusTimer = setInterval(() => this.pollStatus(), 5000);
            }
        },

        // alpha.12: simple Tab/Shift+Tab cycle inside a modal panel. Bound
        // on the wizard via @keydown.tab; cheaper than the Alpine focus plugin
        // and covers the only modal we have today.
        trapFocus(e, container) {
            if (e.key !== 'Tab' || !container) return;
            const focusables = container.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
            );
            if (focusables.length === 0) return;
            const first = focusables[0];
            const last = focusables[focusables.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        },

        openWizard() {
            this.wizardOpen = true;
        },

        closeWizard() {
            this.wizardOpen = false;
        },

        async saveWizard() {
            // saveConfig returns true on success, false on failure (toast already
            // shown). Only close the wizard on a real save.
            const ok = await this.saveConfig();
            if (ok) this.wizardOpen = false;
        },

        async loadConfig() {
            // alpha.12: failures land in loadFailed.config (NOT a shared
            // save-error slot), so a stale load can't be "fixed" by clicking
            // Save and overwriting the real config.
            // alpha.20: fetch draft + live concurrently for per-field diff.
            this.loadFailed.config = '';
            // alpha.20: _suspendAutoSave management is owned by the
            // orchestrators (load, apply, discardAll) — not per-loader.
            // Loaders just write through; the orchestrator wraps the call
            // in suspend so the watcher firings from this.routes = ...
            // assignments don't trigger redundant PUTs.
            try {
                const [draftRes, liveRes] = await Promise.all([
                    fetch(this.url('/api/config')),
                    fetch(this.url('/api/config?live=1')),
                ]);
                if (!draftRes.ok) {
                    const j = await draftRes.json().catch(() => ({}));
                    throw new Error(j.error || `GET /api/config -> ${draftRes.status}`);
                }
                if (liveRes.ok) {
                    const liveJ = await liveRes.json();
                    this.configLive = { ...liveJ };
                }
                const r = draftRes;
                const j = await r.json();
                // cloudflare_token always comes back empty; client treats the
                // input as write-only (placeholder shows ••••• when set).
                this.config = {
                    provider: j.provider || 'cloudflare',
                    cloudflare_token: '',
                    acme_email: j.acme_email || '',
                    domain: j.domain || '',
                    // Phase F: ha_hostname is no longer surfaced (HA system route).
                    entrypoint_http: j.entrypoint_http || 'web',
                    entrypoint_https: j.entrypoint_https || 'websecure',
                    log_level: j.log_level || 'INFO',
                    force_ssl: !!j.force_ssl,
                };
                this.state = j.state || blankState();
            } catch (e) {
                this.loadFailed.config = e.message || String(e);
            }
        },

        async loadRoutes() {
            // alpha.20: fetch draft + live concurrently. Draft binds to the
            // editor; live caches for the per-row diff + soft-delete restore.
            // _suspendAutoSave is set true here and cleared in load() after
            // ALL surface loads complete, so the initial routes assignment
            // doesn't fire a spurious auto-save round-trip.
            this.loadFailed.routes = '';
            // alpha.20: _suspendAutoSave management is owned by the
            // orchestrators (load, apply, discardAll) — not per-loader.
            // Loaders just write through; the orchestrator wraps the call
            // in suspend so the watcher firings from this.routes = ...
            // assignments don't trigger redundant PUTs.
            try {
                const [draftRes, liveRes] = await Promise.all([
                    fetch(this.url('/api/routes')),
                    fetch(this.url('/api/routes?live=1')),
                ]);
                if (!draftRes.ok) {
                    const j = await draftRes.json().catch(() => ({}));
                    throw new Error(j.error || `GET /api/routes -> ${draftRes.status}`);
                }
                if (!liveRes.ok) {
                    const j = await liveRes.json().catch(() => ({}));
                    throw new Error(j.error || `GET /api/routes?live=1 -> ${liveRes.status}`);
                }
                const draft = await draftRes.json();
                const live = await liveRes.json();
                if (draft.domain && !this.config.domain) {
                    this.config.domain = draft.domain;
                }
                this.routes = normalizeRoutes(draft.routes);
                this.routesLive = (live.routes || []).map(r => ({ ...r }));
            } catch (e) {
                this.loadFailed.routes = e.message || String(e);
            }
            // Refresh pending after the surface reloads so the Apply banner
            // accurately reflects disk state (e.g. on the first load after a
            // takeover the inherited draft may already have pending changes).
            this.loadPending().catch(() => {});
        },

        // alpha.20: re-fetch the per-surface diff summary + warnings from
        // disk. Cheap; computed on demand server-side. Called after every
        // successful auto-save flush and after Apply/Discard so the sticky
        // Apply footer stays in sync.
        async loadPending() {
            try {
                const r = await fetch(this.url('/api/pending'));
                if (!r.ok) return;
                this.pending = await r.json();
            } catch (_) { /* drop quietly */ }
        },

        // ---------- alpha.20: auto-save + Apply ----------
        // Debounce window: 500ms covers Most edit patterns. Configuration's
        // regex-gated text fields (cloudflare token, domain) get 1500ms in
        // _autoSaveDelayFor to avoid per-keystroke validation toast spam.
        _autoSaveDelayFor(surface) {
            // alpha.20: tiered debounce. Config is the only surface with
            // strict regex fields where mid-typing snapshots would fail
            // validation on the backend.
            return surface === 'config' ? 1500 : 500;
        },

        // Schedule a debounced auto-save flush for one surface. Called by
        // the Alpine $watch handlers registered in alpine:init. Skipped
        // entirely while _suspendAutoSave is true (set during load()).
        scheduleAutoSave(surface) {
            if (this._suspendAutoSave) return;
            if (this.viewMode === 'ro') return;       // RO observer can't save
            const t = this._autoSaveTimer[surface];
            if (t) clearTimeout(t);
            this._autoSaveTimer[surface] = setTimeout(
                () => this._flushAutoSave(surface),
                this._autoSaveDelayFor(surface),
            );
        },

        async _flushAutoSave(surface) {
            this._autoSaveTimer[surface] = null;
            this._autoSaveInflight[surface] = true;
            try {
                if (surface === 'routes')           await this._putRoutesDraft();
                else if (surface === 'middlewares') await this._putMiddlewaresDraft();
                else if (surface === 'config')      await this._putConfigDraft();
                this.autoSaveError[surface] = '';
                this.loadPending().catch(() => {});
            } catch (e) {
                if (e.code === 'VERSION_MISMATCH') {
                    this.toast.error(
                        'Addon was updated — reload to continue editing.',
                        { sticky: true },
                    );
                    return;
                }
                if (e.code === 'SESSION_LOST') return;   // already handled
                this.autoSaveError[surface] = e.message || String(e);
                this.toast.error(`Auto-save (${surface}) failed: ${e.message}`);
            } finally {
                this._autoSaveInflight[surface] = false;
            }
        },

        async _putRoutesDraft() {
            // Build the same payload shape save() built. Soft-deleted routes
            // are not in this.routes (removeRoute splices) so they're
            // naturally excluded.
            const payload = {
                routes: this.routes.map(r => {
                    const out = {
                        hostname: r.hostname,
                        backend_kind: r.backend_kind,
                        backend_host: r.backend_kind === 'external' ? (r.backend_host || null) : null,
                        backend_port: r.backend_kind === 'external' ? (r.backend_port || null) : null,
                        scheme: r.scheme,
                        tls: !!r.tls,
                        enabled: !!r.enabled,
                        middlewares: r.middlewares || [],
                        health_path: r.health_path || null,
                        skip_tls_verify: !!r.skip_tls_verify,
                    };
                    if (r.rid) out.rid = r.rid;
                    if (r.system) out.system = r.system;
                    return out;
                }),
            };
            await this.api('PUT', '/api/routes', payload);
        },

        async _putMiddlewaresDraft() {
            const payload = {
                middlewares: this.middlewares.map(m => {
                    const out = { name: m.name, type: m.type, config: m.config || {} };
                    if (m.mid) out.mid = m.mid;
                    return out;
                }),
            };
            await this.api('PUT', '/api/middlewares', payload);
        },

        async _putConfigDraft() {
            // Cloudflare token: empty means "keep existing" (backend honors).
            const payload = { ...this.config };
            // Strip blank cloudflare_token (preserve-existing semantic).
            if (!(payload.cloudflare_token || '').trim()) delete payload.cloudflare_token;
            await this.api('PUT', '/api/config', payload);
        },

        // Apply: flush any pending debounce + commit draft → live + render.
        async apply() {
            if (this.applying || this.viewMode === 'ro') return;
            if (this.pending.total === 0) return;
            // Flush any pending debounce timers synchronously first so we
            // don't lose the latest edit.
            for (const s of ['routes', 'middlewares', 'config']) {
                if (this._autoSaveTimer[s]) {
                    clearTimeout(this._autoSaveTimer[s]);
                    this._autoSaveTimer[s] = null;
                    await this._flushAutoSave(s);
                }
            }
            this.applying = true;
            try {
                const j = await this.api('POST', '/api/apply', {});
                if (j && j.ok) {
                    this.toast.success(`Applied ${j.applied} change(s); Traefik reloaded.`);
                    // Live state advanced — refetch everything so the
                    // diff caches + per-row highlights reset cleanly.
                    // Suspend auto-save during the reload to avoid spurious
                    // PUTs from the this.routes = ... reassignment.
                    this._suspendAutoSave = true;
                    await this.loadRoutes();
                    await this.loadMiddlewares();
                    await this.loadConfig();
                    await this.loadPending();
                    await new Promise(r => setTimeout(r, 0));
                    this._suspendAutoSave = false;
                }
            } catch (e) {
                if (e.code === 'VERSION_MISMATCH') {
                    this.toast.error(
                        'Addon was updated — reload to continue editing.',
                        { sticky: true },
                    );
                } else if (e.code !== 'SESSION_LOST') {
                    this.toast.error(`Apply failed: ${e.message}`);
                }
            } finally {
                this.applying = false;
            }
        },

        // Discard all pending changes (resets draft → live). Inline-confirm
        // pattern: first click opens discardConfirmOpen; second click
        // (the "Discard all" button rendered when open) executes.
        async discardAll() {
            if (this.discarding || this.viewMode === 'ro') return;
            this.discarding = true;
            // Cancel any pending debounce timers so a late flush doesn't
            // re-poison the draft after the discard.
            for (const s of ['routes', 'middlewares', 'config']) {
                if (this._autoSaveTimer[s]) {
                    clearTimeout(this._autoSaveTimer[s]);
                    this._autoSaveTimer[s] = null;
                }
            }
            try {
                await this.api('POST', '/api/discard', { scope: 'all' });
                this.toast.success('Discarded pending changes.');
                this.discardConfirmOpen = false;
                // Reload everything so the editor view matches live.
                this._suspendAutoSave = true;
                await this.loadRoutes();
                await this.loadMiddlewares();
                await this.loadConfig();
                await this.loadPending();
                await new Promise(r => setTimeout(r, 0));
                this._suspendAutoSave = false;
            } catch (e) {
                if (e.code !== 'SESSION_LOST') {
                    this.toast.error(`Discard failed: ${e.message}`);
                }
            } finally {
                this.discarding = false;
            }
        },

        // Restore a soft-deleted route — the orphan rows (live routes not
        // present in draft) render with a Restore button instead of Remove.
        // Click pushes the live route back into this.routes; auto-save
        // picks it up.
        restoreRoute(rid) {
            const liveR = this.routesLive.find(r => r.rid === rid);
            if (!liveR) return;
            // Don't duplicate if somehow already present.
            if (this.routes.some(r => r.rid === rid)) return;
            // normalizeRoutes adds default fields + _uid + _expanded; we
            // want the same shape so render is consistent.
            const restored = normalizeRoutes([liveR])[0];
            this.routes.push(restored);
            // No explicit scheduleAutoSave call: the Alpine watcher on
            // this.routes fires on the .push and schedules the flush.
        },

        // Per-row diff helpers used by the Routes table (modified dot etc.).
        isRouteAdded(rid)    { return rid && this.pending.routes.added && this.pending.routes.added.includes(rid); },
        isRouteModified(rid) { return rid && this.pending.routes.modified && this.pending.routes.modified.includes(rid); },
        isRouteDirty(rid)    { return this.isRouteAdded(rid) || this.isRouteModified(rid); },

        // Orphan rows for the Routes table: routes that exist in live but
        // are missing from the draft (= user clicked Remove on them but
        // hasn't Applied yet). Synthesized into the table as struck-through
        // rows with a Restore button.
        get _orphanRoutes() {
            const draftRids = new Set(this.routes.map(r => r.rid).filter(Boolean));
            return this.routesLive
                .filter(r => r.rid && !draftRids.has(r.rid))
                .map(r => ({ ...r, _orphan: true, _expanded: false, _uid: 'orphan-' + r.rid }));
        },

        async pollStatus() {
            // alpha.12: every request carrying X-Session-Id refreshes the
            // server-side heartbeat. We hit /api/status (Traefik check) and
            // /api/state separately; both echo the header so a polling tab
            // keeps its session alive without a dedicated heartbeat endpoint.
            const sidHeader = this.sid ? { 'X-Session-Id': this.sid } : {};
            // alpha.12: track backend reachability via /api/state (our own
            // process, not Traefik). After 3 consecutive failures (15s), surface
            // a sticky "backend unreachable" toast and clear the status dots.
            let backendUp = false;
            try {
                const r = await fetch(this.url('/api/status'), { headers: sidHeader });
                if (r.ok) {
                    this.status = await r.json();
                    this.status.traefik_up = true;
                } else {
                    const body = await r.json().catch(() => ({}));
                    this.status = Object.assign({ traefik_up: false }, body);
                }
            } catch (_) {
                this.status = { traefik_up: false };
            }
            // /api/state isn't UI-visible from here but refreshes server-side
            // last_seen and surfaces banner state changes (integration_pending,
            // trusted_proxies_pending).
            try {
                const rs = await fetch(this.url('/api/state'), { headers: sidHeader });
                if (rs.ok) {
                    this.state = await rs.json();
                    backendUp = true;
                }
            } catch (_) { /* ignore; backendUp stays false */ }
            // alpha.10: per-route backend reachability for the status dots.
            try {
                const rh = await fetch(this.url('/api/route-health'), { headers: sidHeader });
                if (rh.ok) {
                    const j = await rh.json();
                    this.routeHealth = j.health || {};
                }
            } catch (_) { /* keep last-known on a blip */ }

            // Backend-reachability bookkeeping.
            if (backendUp) {
                this._pollFailCount = 0;
                if (this._pollFailToastId) {
                    this.toast.dismiss(this._pollFailToastId);
                    this._pollFailToastId = 0;
                }
            } else {
                this._pollFailCount++;
                if (this._pollFailCount >= 3) {
                    // Reset routeHealth so the dots don't lie green when we
                    // can't actually check.
                    this.routeHealth = {};
                    if (!this._pollFailToastId) {
                        this._pollFailToastId = this.toast.error(
                            'Cannot reach the add-on backend. Status indicators are stale.',
                            { sticky: true }
                        );
                    }
                }
            }
        },

        // alpha.10: reachability of a route's backend, keyed by raw hostname.
        // up = backend healthy; down = backend unreachable; disabled = route off;
        // unknown = Traefik down / healthCheck not yet run / route not rendered.
        routeStatus(route) {
            return this.routeHealth[route.hostname] || 'unknown';
        },

        routeStatusColor(route) {
            const s = this.routeStatus(route);
            if (s === 'up') return 'bg-green-500';
            if (s === 'down') return 'bg-red-500';
            return 'bg-gray-300';  // unknown / disabled
        },

        routeStatusTitle(route) {
            const s = this.routeStatus(route);
            if (s === 'up') return 'Backend reachable';
            if (s === 'down') return 'Backend unreachable';
            if (s === 'disabled') return 'Route disabled';
            return 'Status unknown (Traefik down or health check pending)';
        },

        async saveConfig() {
            this.savingConfig = true;
            const payload = {
                provider: this.config.provider,
                // Empty token = "keep existing" (backend preserves the
                // currently-stored value). Non-empty = overwrite.
                cloudflare_token: this.config.cloudflare_token || '',
                acme_email: this.config.acme_email,
                domain: this.config.domain,
                // Phase F: ha_hostname dropped from the payload; HA system
                // route in the Routes tab owns the subdomain now.
                entrypoint_http: this.config.entrypoint_http,
                entrypoint_https: this.config.entrypoint_https,
                log_level: this.config.log_level,
                force_ssl: !!this.config.force_ssl,
            };
            try {
                const j = await this.api('PUT', '/api/config', payload);
                this.state = j.state || blankState();
                this.restartRequired = !!j.restart_required;
                // Wipe the token input back to empty: it's been saved
                // (or preserved); the placeholder switches to ••••• now.
                this.config.cloudflare_token = '';
                this.toast.success('Configuration saved.');
                return true;
            } catch (e) {
                if (e.code !== 'SESSION_LOST') {
                    this.toast.error(`Couldn't save configuration: ${e.message}`);
                }
                return false;
            } finally {
                this.savingConfig = false;
            }
        },

        async restartCore() {
            // Shared by all three restart triggers (integration update_pending,
            // integration available/discovery, trusted_proxies fix). POST
            // /api/restart-core; backend proxies to supervisor with retry+backoff.
            // HA Core takes ~30-90s to come back.
            //
            // alpha.6 poll fix: every browser->add-on request traverses HA Core
            // ingress (:8123), so when Core restarts the poll fetch FAILS. Wait
            // for HA to actually go down and come back (sawDown) rather than for
            // a marker flag — State B (available) never sets a pending flag, so
            // the old "exit when !integration_pending_restart" condition fired on
            // the first poll (before Core even went down) and reloaded too early.
            this.restartingCore = true;
            try {
                const r = await fetch(this.url('/api/restart-core'), {
                    method: 'POST',
                    headers: this.sid ? { 'X-Session-Id': this.sid } : {},
                });
                if (r.status === 423) { this.onSessionLost(); return; }
                if (!r.ok) {
                    const j = await r.json().catch(() => ({}));
                    throw new Error(j.error || `POST /api/restart-core -> ${r.status}`);
                }
                const start = Date.now();
                let sawDown = false;
                while (Date.now() - start < 120000) {
                    await new Promise(r => setTimeout(r, 2000));
                    let up = false;
                    try {
                        const r2 = await fetch(this.url('/api/state'), { cache: 'no-store' });
                        up = r2.ok;
                    } catch (_) { up = false; }
                    if (!up) {
                        sawDown = true;          // Core (or its ingress) is down
                    } else if (sawDown) {
                        // Down then back up = restart completed. Reload to re-sync.
                        window.location.reload();
                        return;
                    }
                }
                this.toast.error('Restart timed out after 120s. Refresh the page manually.');
            } catch (e) {
                this.toast.error(`Restart failed: ${e.message}`);
            } finally {
                this.restartingCore = false;
            }
        },

        async fixTrustedProxies() {
            // alpha.6: POST /api/fix-trusted-proxies. On success the backend has
            // edited configuration.yaml; the trustedProxiesFixed banner stays
            // until the user clicks Restart HA Core.
            this.fixingTrustedProxies = true;
            this.trustedProxiesFixed = false;
            try {
                const r = await fetch(this.url('/api/fix-trusted-proxies'), {
                    method: 'POST',
                    headers: this.sid ? { 'X-Session-Id': this.sid } : {},
                });
                if (r.status === 423) { this.onSessionLost(); return; }
                if (!r.ok) {
                    const j = await r.json().catch(() => ({}));
                    throw new Error(j.error || `POST /api/fix-trusted-proxies -> ${r.status}`);
                }
                this.trustedProxiesFixed = true;
                // Detection re-reads the file; the pending banner clears on next poll.
                this.state.trusted_proxies_pending = false;
                this.toast.success('configuration.yaml updated; restart HA Core to apply.');
            } catch (e) {
                this.toast.error(`Couldn't update configuration.yaml: ${e.message}`);
            } finally {
                this.fixingTrustedProxies = false;
            }
        },

        async dismissIntegration() {
            // alpha.6: hide the "integration available" (State B) banner for the
            // current integration content. Persisted server-side (content-scoped),
            // so a future integration version re-surfaces it.
            this.dismissingIntegration = true;
            try {
                const r = await fetch(this.url('/api/dismiss-integration'), {
                    method: 'POST',
                    headers: this.sid ? { 'X-Session-Id': this.sid } : {},
                });
                if (r.status === 423) {
                    this.onSessionLost();
                } else if (r.ok) {
                    this.state.integration_available = false;
                } else {
                    // alpha.12: dismiss no longer silently swallows non-2xx.
                    const j = await r.json().catch(() => ({}));
                    this.toast.error(j.error || `Couldn't dismiss banner: HTTP ${r.status}`);
                }
            } catch (e) {
                this.toast.error(`Couldn't dismiss banner: ${e.message}`);
            }
            finally {
                this.dismissingIntegration = false;
            }
        },

        async restartAddon() {
            this.restarting = true;
            try {
                // The supervisor may kill our backend mid-flight; treat
                // network errors as the success path (request was sent).
                await fetch(this.url('/api/restart'), {
                    method: 'POST',
                    headers: this.sid ? { 'X-Session-Id': this.sid } : {},
                }).catch(() => null);
                // Poll /api/status every 1s until the backend comes back.
                // ~5-15s typical; bail at 60s.
                const start = Date.now();
                while (Date.now() - start < 60000) {
                    await new Promise(r => setTimeout(r, 1000));
                    try {
                        const r = await fetch(this.url('/api/status'));
                        // Even a 503 from us means we're back up; 503 just
                        // means Traefik isn't bound yet, which clears quickly.
                        if (r.status === 200 || r.status === 503) {
                            window.location.reload();
                            return;
                        }
                    } catch (_) { /* still down; keep polling */ }
                }
                this.toast.error('Restart timed out after 60s. Refresh the page manually.');
            } catch (e) {
                this.toast.error(`Restart failed: ${e.message}`);
            } finally {
                this.restarting = false;
            }
        },

        addRoute() {
            this.routes.push(makeBlankRoute());
        },

        // Phase F: takes the route OBJECT (template iterates sortedRoutes,
        // not the underlying array, so an index would be wrong). System rows
        // hide the Remove button via x-show, but belt+braces guard here too.
        removeRoute(r) {
            if (r && r.system) return;
            const i = this.routes.indexOf(r);
            if (i >= 0) this.routes.splice(i, 1);
        },

        async save() {
            if (this.systemRowInvalid) {
                this.toast.error('HA system route has empty hostname; type one before saving (or disable it).');
                return;
            }
            this.saving = true;
            const payload = {
                routes: this.routes.map(r => {
                    const out = {
                        hostname: r.hostname,
                        backend_kind: r.backend_kind,
                        backend_host: r.backend_kind === 'external' ? (r.backend_host || null) : null,
                        backend_port: r.backend_kind === 'external' ? (r.backend_port || null) : null,
                        scheme: r.scheme,
                        tls: !!r.tls,
                        enabled: !!r.enabled,
                        middlewares: r.middlewares || [],
                        // Phase E: preserve hand-edited health_path through the UI round-trip.
                        // No input control in v1; this just keeps the field from being stripped.
                        health_path: r.health_path || null,
                        // alpha.15: include the per-route bool. Omitting it had the
                        // server's LOCKED-set check on the HA system route compare
                        // `None != False` → reject every Routes-tab Save with
                        // "system route field 'skip_tls_verify' is locked".
                        skip_tls_verify: !!r.skip_tls_verify,
                    };
                    // alpha.20: round-trip the server-assigned `rid`. Backend
                    // uses it to key the draft-vs-live diff for per-row
                    // change tracking; if the frontend ever dropped it on
                    // save, the server would re-generate a fresh rid and
                    // every save would look like add+delete in the pending
                    // changes UI. Lesson from alpha.15 — hand-rolled
                    // serializers drift; this list MUST grow with the model.
                    if (r.rid) out.rid = r.rid;
                    // Phase F: preserve system tag so backend's protection
                    // check matches; user routes omit the field entirely
                    // (sending null would trigger "unknown kind" elsewhere).
                    if (r.system) out.system = r.system;
                    return out;
                    // NB: _uid is NOT in the payload -- it's a local rendering-key only.
                }),
            };
            try {
                const j = await this.api('PUT', '/api/routes', payload);
                this.toast.success(`Saved ${j.saved} route(s); Traefik reloaded.`);
            } catch (e) {
                if (e.code !== 'SESSION_LOST') {
                    this.toast.error(`Couldn't save routes: ${e.message}`);
                }
            } finally {
                this.saving = false;
            }
        },

        // ---------- Phase F: middlewares CRUD ----------
        async loadMiddlewares() {
            // alpha.20: fetch draft + live concurrently. Same shape as
            // loadRoutes — draft binds to the editor, live caches for diff.
            this.loadFailed.middlewares = '';
            // alpha.20: _suspendAutoSave management is owned by the
            // orchestrators (load, apply, discardAll) — not per-loader.
            // Loaders just write through; the orchestrator wraps the call
            // in suspend so the watcher firings from this.routes = ...
            // assignments don't trigger redundant PUTs.
            try {
                const [draftRes, liveRes] = await Promise.all([
                    fetch(this.url('/api/middlewares')),
                    fetch(this.url('/api/middlewares?live=1')),
                ]);
                if (!draftRes.ok) {
                    const j = await draftRes.json().catch(() => ({}));
                    throw new Error(j.error || `GET /api/middlewares -> ${draftRes.status}`);
                }
                if (!liveRes.ok) {
                    const j = await liveRes.json().catch(() => ({}));
                    throw new Error(j.error || `GET /api/middlewares?live=1 -> ${liveRes.status}`);
                }
                const draft = await draftRes.json();
                const live = await liveRes.json();
                this.middlewares = normalizeMiddlewares(draft.middlewares);
                this.middlewaresLive = (live.middlewares || []).map(m => ({ ...m }));
            } catch (e) {
                this.loadFailed.middlewares = e.message || String(e);
            }
            this.loadPending().catch(() => {});
        },

        // alpha.15: middlewares to render as chips on the HA system row
        // (read-only). Filters out any middleware whose feature-managed
        // hideFromDropdown predicate matches the current config — currently:
        // redirect-to-https when force_ssl is on (render uses an
        // entrypoint-level redirect, so the chip is misleading). Same
        // FEATURE_MANAGED_MIDDLEWARES predicate as the user-route dropdown,
        // one source of truth for "this middleware is owned by a feature
        // toggle elsewhere; don't surface it here."
        systemRowVisibleMiddlewares(route) {
            return (route.middlewares || []).filter(name => {
                const rule = FEATURE_MANAGED_MIDDLEWARES[name];
                return !(rule && rule.hideFromDropdown(this.config));
            });
        },

        // alpha.7: toggle a middleware on/off for a user route (multiselect).
        // Append/remove preserves insertion order (Traefik applies in list order).
        toggleRouteMiddleware(route, name, checked) {
            if (!Array.isArray(route.middlewares)) route.middlewares = [];
            const i = route.middlewares.indexOf(name);
            if (checked && i === -1) route.middlewares.push(name);
            else if (!checked && i !== -1) route.middlewares.splice(i, 1);
        },

        // alpha.14: middlewares selectable in a route's dropdown — drops every
        // FEATURE_MANAGED_MIDDLEWARES entry whose hideFromDropdown predicate
        // returns true for the current config (currently: redirect-to-https
        // when force_ssl is on; the entrypoint-level redirect supersedes it).
        eligibleRouteMiddlewares() {
            return this.middlewares.filter(m => {
                const rule = FEATURE_MANAGED_MIDDLEWARES[m.name];
                return !(rule && rule.hideFromDropdown(this.config));
            });
        },

        // alpha.14: middlewares shown in the Middlewares tab list — drops
        // every entry whose hideFromTab predicate returns true (currently:
        // redirect-to-https unconditionally; user-managed via route dropdown +
        // force_ssl toggle, not a Tab row). The migration step also removes
        // skip-tls-verify entirely so it never reaches the UI.
        get visibleMiddlewares() {
            return this.middlewares.filter(m => {
                const rule = FEATURE_MANAGED_MIDDLEWARES[m.name];
                return !(rule && rule.hideFromTab(this.config));
            });
        },

        // Summary text for the compact route-middlewares dropdown trigger.
        routeMwSummary(route) {
            const names = route.middlewares || [];
            return names.length ? names.join(', ') : 'No middlewares';
        },

        // alpha.14: skip-TLS-verify only applies to https backends; clear the
        // bool when the route's scheme is changed away from https, and surface
        // a toast so the user can see WHY their checkbox state changed.
        onRouteSchemeChange(route) {
            if (route.scheme !== 'https' && route.skip_tls_verify) {
                route.skip_tls_verify = false;
                this.toast.info('Skip TLS verify turned off — it only applies to https backends.');
            }
        },

        addMiddleware() {
            this.middlewares.push(makeBlankMiddleware());
        },

        removeMiddleware(m) {
            if (m.system) return;  // built-ins can't be removed (server re-injects anyway)
            const i = this.middlewares.indexOf(m);
            if (i >= 0) this.middlewares.splice(i, 1);
        },

        // alpha.12: replace the native confirm() with an inline confirmation
        // step rendered inside the middleware card. confirm() blocks the event
        // loop, looks alien inside the HA frontend, and isn't keyboard-friendly.
        // The card now stores `_pendingType` while awaiting the user's choice.
        changeMiddlewareType(m, newType) {
            if (m.system) return;  // built-in type is locked
            if (newType === m.type) return;
            const hasContent = JSON.stringify(m.config) !== JSON.stringify(emptyConfigFor(m.type));
            if (hasContent) {
                m._pendingType = newType;     // template renders the inline confirm strip
                return;
            }
            this._applyTypeChange(m, newType);
        },
        confirmTypeChange(m) {
            if (!m._pendingType) return;
            this._applyTypeChange(m, m._pendingType);
            m._pendingType = null;
        },
        cancelTypeChange(m) {
            m._pendingType = null;
        },
        _applyTypeChange(m, newType) {
            m.type = newType;
            // For basicAuth the empty shape is {users: []}; the UI's per-user
            // local state (_orig_username etc.) is set on add-user, not here.
            m.config = emptyConfigFor(newType);
        },

        addBasicAuthUser(m) {
            if (!m.config.users) m.config.users = [];
            m.config.users.push({
                username: '',
                password: '',
                password_set: false,
                // no _orig_username -> backend treats as new user, requires password
            });
        },

        removeBasicAuthUser(m, u) {
            const i = m.config.users.indexOf(u);
            if (i >= 0) m.config.users.splice(i, 1);
        },

        addAllowRange(m) {
            if (!m.config.sourceRange) m.config.sourceRange = [];
            m.config.sourceRange.push('');
        },

        removeAllowRange(m, idx) {
            m.config.sourceRange.splice(idx, 1);
        },

        addHeaderRow(m, side) {
            // side: 'customRequestHeaders' or 'customResponseHeaders'
            if (!m.config[side]) m.config[side] = [];
            m.config[side].push({ key: '', value: '' });
        },

        removeHeaderRow(m, side, idx) {
            m.config[side].splice(idx, 1);
        },

        async saveMiddlewares() {
            this.savingMw = true;
            const payload = {
                middlewares: this.middlewares.map(m => {
                    const out = { name: m.name, type: m.type };
                    if (m.type === 'basicAuth') {
                        out.config = {
                            users: (m.config.users || []).map(u => {
                                const row = {
                                    username: u.username,
                                };
                                // Send password ONLY when user typed one
                                // (blank means "keep existing hash").
                                if (u.password) row.password = u.password;
                                // _orig_username present iff the user came from GET
                                // (existing user); backend uses this to match hashes
                                // and to reject rename+blank-password.
                                if (u._orig_username !== undefined && u._orig_username !== '') {
                                    row._orig_username = u._orig_username;
                                }
                                return row;
                            }),
                        };
                    } else if (m.type === 'ipAllowList') {
                        out.config = {
                            sourceRange: (m.config.sourceRange || [])
                                .map(s => (s || '').trim()).filter(Boolean),
                        };
                    } else if (m.type === 'redirectScheme') {
                        out.config = {
                            scheme: m.config.scheme || 'https',
                            permanent: !!m.config.permanent,
                        };
                    } else if (m.type === 'headers') {
                        out.config = {
                            customRequestHeaders: rowsToDict(m.config.customRequestHeaders),
                            customResponseHeaders: rowsToDict(m.config.customResponseHeaders),
                        };
                    }
                    return out;
                }),
            };
            try {
                const j = await this.api('PUT', '/api/middlewares', payload);
                this.toast.success(`Saved ${j.saved} middleware(s); Traefik reloaded.`);
                // Re-load to pick up server-side normalisations (CIDR rewrite,
                // hashed passwords, _orig_username refresh).
                await this.loadMiddlewares();
            } catch (e) {
                if (e.code !== 'SESSION_LOST') {
                    this.toast.error(`Couldn't save middlewares: ${e.message}`);
                }
            } finally {
                this.savingMw = false;
            }
        },

        url(path) {
            return (this.ingressPath || '') + path;
        },

        // alpha.20: getter-backed snapshots used as $watch targets.
        // Alpine 3's $watch on a bare property name ('routes') only fires
        // on shallow ref changes — NOT on nested edits to
        // routes[i].hostname. By routing through these getters, every
        // nested field becomes a reactive dependency of the getter; the
        // string output changes on any meaningful mutation; $watch fires.
        // The replacer strips `_`-prefixed UI-state fields (`_uid`,
        // `_expanded`, `_orphan`) so a chevron toggle doesn't trigger an
        // auto-save PUT.
        get _routesSnapshot() {
            return JSON.stringify(this.routes,
                (k, v) => k.startsWith('_') ? undefined : v);
        },
        get _middlewaresSnapshot() {
            return JSON.stringify(this.middlewares,
                (k, v) => k.startsWith('_') ? undefined : v);
        },
        get _configSnapshot() {
            return JSON.stringify(this.config);
        },

        // alpha.20: Alpine auto-invokes init() before x-init="load()"; the
        // watchers register against the snapshot getters above.
        init() {
            this.$watch('_routesSnapshot',      () => this.scheduleAutoSave('routes'));
            this.$watch('_middlewaresSnapshot', () => this.scheduleAutoSave('middlewares'));
            this.$watch('_configSnapshot',      () => this.scheduleAutoSave('config'));
        },
    };
}

// Register with Alpine via the alpine:init event. Canonical Alpine 3.x
// pattern; race-free with respect to script load order.
document.addEventListener('alpine:init', () => {
    window.Alpine.data('traefikApp', traefikAppData);
});

// Back-compat for cached HTML that still uses x-data="traefikApp()".
window.traefikApp = traefikAppData;
