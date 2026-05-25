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
        // makeBlankRoute does NOT set `system` -- user routes only.
        // The HA system route is seeded by migrate.py and persisted in routes.yml.
    };
}

function normalizeRoutes(routes) {
    return (routes || []).map(r => Object.assign(makeBlankRoute(), r, {
        _uid: ++_routeUid,                 // fresh uid every load
        middlewares: Array.isArray(r.middlewares) ? r.middlewares : [],
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
        // skipTlsVerify: no config ({}). system flag (add-on built-in) is
        // server-derived; the UI uses it to gray/lock the card.
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
        ingressPath: INGRESS_PATH,
        // Default 'routes' once configured; load() flips to 'config'
        // (with wizardOpen=true) on first run so a sensible underlay
        // sits behind the wizard overlay.
        tab: 'routes',
        dashboardLoaded: false,

        // Configuration tab + wizard overlay share the same config object.
        // wizardOpen is shown on first-load when !configured, or when the
        // user clicks "Re-run setup wizard" on the Configuration page.
        config: blankConfig(),
        state: blankState(),
        wizardOpen: false,
        savingConfig: false,
        setupError: '',
        setupOk: '',
        restartRequired: false,
        restarting: false,
        restartError: '',

        // Phase 4 (0.9.0): HA Core restart triggered from the integration-
        // deploy banner. Distinct state from the addon-self-restart above so
        // both flows can run independently without UI confusion.
        restartingCore: false,
        restartCoreError: '',

        // alpha.6: trusted_proxies quick-fix banner.
        fixingTrustedProxies: false,
        trustedProxiesError: '',
        trustedProxiesFixed: false,
        showTpSnippet: false,
        // alpha.6: dismiss the "integration available" (State B) banner.
        dismissingIntegration: false,

        // Routes tab
        routes: [],
        saving: false,
        saveError: '',
        saveOk: '',
        lastSavedAt: '',

        // Phase F: Middlewares tab
        middlewares: [],
        savingMw: false,
        mwError: '',
        mwOk: '',

        // Dashboard / status badge
        status: {},
        _statusTimer: null,
        // alpha.10: per-route backend reachability (hostname -> up|down|unknown|
        // disabled), refreshed by pollStatus; drives the status dot per route.
        routeHealth: {},

        // Phase F: system routes render first in the table (and a colliding
        // user route would lose; see render.py for the matching invariant).
        get sortedRoutes() {
            const sys = this.routes.filter(r => r.system);
            const usr = this.routes.filter(r => !r.system);
            return [...sys, ...usr];
        },

        // Phase F: block Save when an enabled system route has empty hostname
        // (renderer would skip it, making HA unreachable through Traefik).
        get systemRowInvalid() {
            return this.routes.some(
                r => r.system && r.enabled && !(r.hostname || '').trim()
            );
        },

        async load() {
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
            this.pollStatus();
            if (!this._statusTimer) {
                this._statusTimer = setInterval(() => this.pollStatus(), 5000);
            }
        },

        openWizard() {
            this.setupError = '';
            this.setupOk = '';
            this.wizardOpen = true;
        },

        closeWizard() {
            this.wizardOpen = false;
        },

        async saveWizard() {
            await this.saveConfig();
            // saveConfig sets setupError on failure; only close the wizard
            // when the save actually succeeded.
            if (!this.setupError) {
                this.wizardOpen = false;
            }
        },

        async loadConfig() {
            this.setupError = '';
            this.setupOk = '';
            try {
                const r = await fetch(this.url('/api/config'));
                if (!r.ok) throw new Error(`GET /api/config -> ${r.status}`);
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
                this.setupError = `Failed to load config: ${e}`;
            }
        },

        async loadRoutes() {
            this.saveError = '';
            this.saveOk = '';
            try {
                const r = await fetch(this.url('/api/routes'));
                if (!r.ok) throw new Error(`GET /api/routes -> ${r.status}`);
                const j = await r.json();
                if (j.domain && !this.config.domain) {
                    this.config.domain = j.domain;
                }
                this.routes = normalizeRoutes(j.routes);
            } catch (e) {
                this.saveError = `Failed to load routes: ${e}`;
            }
        },

        async pollStatus() {
            try {
                const r = await fetch(this.url('/api/status'));
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
            // alpha.10: per-route backend reachability for the status dots.
            try {
                const rh = await fetch(this.url('/api/route-health'));
                if (rh.ok) {
                    const j = await rh.json();
                    this.routeHealth = j.health || {};
                }
            } catch (_) { /* keep last-known on a blip */ }
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
            this.setupError = '';
            this.setupOk = '';
            this.restartRequired = false;
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
                const r = await fetch(this.url('/api/config'), {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                if (!r.ok) {
                    const text = await r.text();
                    throw new Error(`PUT /api/config -> ${r.status}: ${text}`);
                }
                const j = await r.json();
                this.state = j.state || blankState();
                this.restartRequired = !!j.restart_required;
                // Wipe the token input back to empty: it's been saved
                // (or preserved); the placeholder switches to ••••• now.
                this.config.cloudflare_token = '';
                this.setupOk = 'Configuration saved.';
            } catch (e) {
                this.setupError = String(e);
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
            this.restartCoreError = '';
            try {
                const r = await fetch(this.url('/api/restart-core'), { method: 'POST' });
                if (!r.ok) {
                    const text = await r.text();
                    throw new Error(`POST /api/restart-core -> ${r.status}: ${text}`);
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
                this.restartCoreError = 'Restart timed out after 120s. Refresh the page manually.';
            } catch (e) {
                this.restartCoreError = String(e);
            } finally {
                this.restartingCore = false;
            }
        },

        async fixTrustedProxies() {
            // alpha.6: POST /api/fix-trusted-proxies. On success the backend has
            // edited configuration.yaml; surface the "restart to apply" banner.
            // On a 4xx bail the body carries the reason + manual snippet.
            this.fixingTrustedProxies = true;
            this.trustedProxiesError = '';
            this.trustedProxiesFixed = false;
            try {
                const r = await fetch(this.url('/api/fix-trusted-proxies'), { method: 'POST' });
                if (!r.ok) {
                    const text = await r.text();
                    throw new Error(text || `POST /api/fix-trusted-proxies -> ${r.status}`);
                }
                this.trustedProxiesFixed = true;
                // Detection re-reads the file; the pending banner clears on next poll.
                this.state.trusted_proxies_pending = false;
            } catch (e) {
                this.trustedProxiesError = String(e);
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
                const r = await fetch(this.url('/api/dismiss-integration'), { method: 'POST' });
                if (r.ok) {
                    this.state.integration_available = false;
                }
            } catch (_) { /* best-effort; banner returns on next load if it failed */ }
            finally {
                this.dismissingIntegration = false;
            }
        },

        async restartAddon() {
            this.restarting = true;
            this.restartError = '';
            try {
                // The supervisor may kill our backend mid-flight; treat
                // network errors as the success path (request was sent).
                await fetch(this.url('/api/restart'), { method: 'POST' })
                    .catch(() => null);
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
                this.restartError = 'Restart timed out after 60s. Refresh the page manually.';
            } catch (e) {
                this.restartError = `Restart failed: ${e}`;
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
                this.saveError = 'HA system route has empty hostname; type one before saving (or disable it).';
                return;
            }
            this.saving = true;
            this.saveError = '';
            this.saveOk = '';
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
                    };
                    // Phase F: preserve system tag so backend's protection
                    // check matches; user routes omit the field entirely
                    // (sending null would trigger "unknown kind" elsewhere).
                    if (r.system) out.system = r.system;
                    return out;
                    // NB: _uid is NOT in the payload -- it's a local rendering-key only.
                }),
            };
            try {
                const r = await fetch(this.url('/api/routes'), {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                if (!r.ok) {
                    const text = await r.text();
                    throw new Error(`PUT /api/routes -> ${r.status}: ${text}`);
                }
                const j = await r.json();
                this.saveOk = `Saved ${j.saved} route(s); Traefik reloaded.`;
                this.lastSavedAt = new Date().toLocaleTimeString();
            } catch (e) {
                this.saveError = String(e);
            } finally {
                this.saving = false;
            }
        },

        // ---------- Phase F: middlewares CRUD ----------
        async loadMiddlewares() {
            this.mwError = '';
            this.mwOk = '';
            try {
                const r = await fetch(this.url('/api/middlewares'));
                if (!r.ok) throw new Error(`GET /api/middlewares -> ${r.status}`);
                const j = await r.json();
                this.middlewares = normalizeMiddlewares(j.middlewares);
            } catch (e) {
                this.mwError = `Failed to load middlewares: ${e}`;
            }
        },

        // alpha.7: toggle a middleware on/off for a user route (multiselect).
        // Append/remove preserves insertion order (Traefik applies in list order).
        toggleRouteMiddleware(route, name, checked) {
            if (!Array.isArray(route.middlewares)) route.middlewares = [];
            const i = route.middlewares.indexOf(name);
            if (checked && i === -1) route.middlewares.push(name);
            else if (!checked && i !== -1) route.middlewares.splice(i, 1);
        },

        // alpha.9: middlewares selectable in a route's dropdown. Excludes
        // skip-tls-verify (its own per-route checkbox when scheme=https) and
        // redirect-to-https when Force SSL is on (applied globally instead).
        eligibleRouteMiddlewares() {
            return this.middlewares.filter(m => {
                if (m.name === 'skip-tls-verify') return false;
                if (m.name === 'redirect-to-https' && this.config.force_ssl) return false;
                return true;
            });
        },

        // alpha.9: summary text for the compact route-middlewares dropdown
        // (skip-tls-verify is shown via the scheme checkbox, not here).
        routeMwSummary(route) {
            const names = (route.middlewares || []).filter(n => n !== 'skip-tls-verify');
            return names.length ? names.join(', ') : 'No middlewares';
        },

        // alpha.9: skip-tls-verify only applies to https backends; drop it when
        // the route's scheme is changed away from https.
        onRouteSchemeChange(route) {
            if (route.scheme !== 'https') {
                this.toggleRouteMiddleware(route, 'skip-tls-verify', false);
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

        changeMiddlewareType(m, newType) {
            if (m.system) return;  // built-in type is locked
            if (newType === m.type) return;
            // If the user has typed anything into the current config, confirm
            // before discarding. Use a heuristic: any populated array OR any
            // non-default scalar.
            const hasContent = JSON.stringify(m.config) !== JSON.stringify(emptyConfigFor(m.type));
            if (hasContent && !confirm(
                'Changing middleware type will discard the current configuration. Continue?'
            )) {
                return;
            }
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
            this.mwError = '';
            this.mwOk = '';
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
                const r = await fetch(this.url('/api/middlewares'), {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                if (!r.ok) {
                    const text = await r.text();
                    throw new Error(`PUT /api/middlewares -> ${r.status}: ${text}`);
                }
                const j = await r.json();
                this.mwOk = `Saved ${j.saved} middleware(s); Traefik reloaded.`;
                // Re-load to pick up server-side normalisations (CIDR rewrite,
                // hashed passwords, _orig_username refresh).
                await this.loadMiddlewares();
            } catch (e) {
                this.mwError = String(e);
            } finally {
                this.savingMw = false;
            }
        },

        url(path) {
            return (this.ingressPath || '') + path;
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
