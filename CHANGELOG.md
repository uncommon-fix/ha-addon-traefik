# Changelog

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
