# Changelog

## 0.1.0-alpha.7

- New: a built-in **`skip-tls-verify`** middleware. Attach it to a route whose
  backend is HTTPS with a self-signed/untrusted certificate (e.g. a LAN app) and
  Traefik will stop verifying that backend's cert.
- Routes tab: the middlewares column is now a **multiselect** of your configured
  middlewares (no more comma-typing), and the Home Assistant system route now
  **lists** its applied middlewares (e.g. `redirect-to-https`) instead of showing
  a dash.
- The two built-in middlewares (`redirect-to-https`, `skip-tls-verify`) are shown
  as **Built-in** in the Middlewares tab: their config can still be edited where
  applicable, but they can't be renamed, retyped, or removed.

## 0.1.0-alpha.6

- New: a **Fix automatically** button for the HTTPS-returns-400 problem. When
  `configuration.yaml` is missing the `trusted_proxies` / `use_x_forwarded_for`
  settings that let Home Assistant trust the Traefik proxy, the add-on shows a
  banner and can add them for you (it edits `configuration.yaml` in place,
  preserving comments, makes a `configuration.yaml.traefik-addon.bak` backup,
  and leaves split configs using `!include`/`!secret` untouched). Restart Home
  Assistant afterwards to apply.
- Fixed: the "reachability integration" banner no longer nags forever before you
  add the integration. It now distinguishes "available — add it (optional)" from
  "updated — restart to load," the available one has a **Dismiss** button, and
  the restart button no longer falsely times out.

## 0.1.0-alpha.5

- HTTP now redirects to HTTPS automatically. A `redirect-to-https` middleware
  ships by default and is applied to the Home Assistant route, so
  `http://<your-domain>/` 308-redirects to `https://`. It also back-fills onto
  existing installs. (The redirect is a permanent/308 — if you toggle a route's
  TLS off, clear your browser cache.)
- Fixed: an add-on update that doesn't change the bundled integration no longer
  shows a spurious "restart Home Assistant" prompt.

## 0.1.0-alpha.4

- "Restart Home Assistant to load the updated integration" is now also surfaced
  as an official **Settings → System → Repairs** card (one-click restart), in
  addition to the add-on banner.
- The banner now clears automatically after a restart from **any** source (the
  Repairs card, a manual restart, etc.), not only the add-on's own button.
- Restart prompts are now based on the integration's actual content, so add-on
  releases that don't change the integration no longer ask for a restart.
- Fixed a harmless `runtime_data` AttributeError logged by the integration on
  startup.

## 0.1.0-alpha.3

- Fix: the setup wizard failed to save with a 500 error (`PUT /api/config`).
  Config validation no longer crashes when the optional `ha_hostname` field is
  absent from the payload.

## 0.1.0-alpha.2

- Removed the supervisor **Configuration** form. All settings (routes,
  middlewares, TLS, entry points, log level) live in the add-on's **Web UI**
  ("Open Web UI"); the supervisor now shows only the **Network** section for
  port mapping.
- After updating, a one-time "Restart HA Core" banner appears (the bundled
  reachability integration redeploys on the version bump).

## 0.1.0-alpha.1

Initial public alpha. Expect breaking changes between releases.

- LAN reverse-proxy powered by Traefik 3.7.1 on ports 80/443.
- Built-in UI (Home Assistant ingress) for routes and middlewares (basicAuth,
  ipAllowList, redirectScheme, headers), plus a setup wizard.
- TLS via Cloudflare DNS-01 ACME (Let's Encrypt).
- Read-only embedded view of the Traefik dashboard.
- Bundled reachability integration: publishes one
  `binary_sensor.traefik_route_<slug>_reachable` per route (connectivity
  device class) for use in automations. A removed route degrades its sensor to
  `unavailable` rather than deleting it.
- Multi-arch images (aarch64, amd64) published to GitHub Container Registry.
