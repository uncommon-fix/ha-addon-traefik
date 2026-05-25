# Traefik add-on

## What this does

LAN reverse-proxy add-on. Bundles Traefik 3.7.1 on `:80`/`:443` with ACME via Cloudflare DNS-01. Routes, middlewares, and cert config are managed in the add-on's own UI (**Open Web UI**), not the supervisor's Configure tab. HAOS-native, single-instance, internal LAN scope — not designed for arbitrary internet exposure.

### State

- `/data/config.yml` — core config (provider, ACME email, domain, Cloudflare token, log level, entry-point names).
- `/data/routes.yml` — routes, including the HA system row (`system: ha_self`).
- `/data/middlewares.yml` — middleware definitions (basicAuth / ipAllowList / redirectScheme / headers).
- `/data/acme.json` — Let's Encrypt certs cache (managed by Traefik).

The UI is source-of-truth. Hand-edit `/data/*.yml` only when the UI is broken.

## Install

1. Settings → Add-ons → Add-on Store → **⋮** (top-right) → **Repositories** → add
   `https://github.com/uncommon-fix/ha-addons`.
2. Find **Traefik** in the store → Install → Start.
3. **Open Web UI** for the setup wizard.

## Tab notes (gotchas only — UI labels the rest)

- **Routes tab — system row.** The first row is pinned, marked "System" (the HA backend). Only its hostname is editable; everything else is locked.
- **Middlewares tab — basicAuth hashes.** The Traefik dashboard exposes the stored bcrypt hashes in middleware definitions. This is by design — bcrypt is a one-way hash; the dashboard isn't leaking anything reversible.
- **Middlewares tab — headers empty value.** Setting a header value to empty string SETS the header to empty (Traefik semantics). To DELETE a header, remove the entire row — its key drops out of the saved map.
- **Traefik dashboard tab.** Read-only iframe of Traefik's own dashboard. No edits flow back from here.

## First-run footnotes

- **Cloudflare API token** needs `Zone:Zone:Read` + `Zone:DNS:Edit` on the target zone. A token without DNS:Edit will silently fail at ACME challenge.
- **ACME contact email** must be a real domain. Let's Encrypt rejects `example.com` (and similar LE-blocked test domains) with `urn:ietf:params:acme:error:invalidContact`.

## Troubleshooting

- **HTTPS / HA-backend route returns HTTP 400** → HA Core rejects proxied requests from untrusted sources. The add-on detects this and shows a **Fix automatically** banner that adds `use_x_forwarded_for: true` + `trusted_proxies: [172.30.32.0/23]` (the supervisor docker network) to your `configuration.yaml` for you (a `configuration.yaml.traefik-addon.bak` backup is written; comments and `!include`/`!secret` are preserved). Restart Home Assistant afterwards. If your `http:` block is split out via `!include`, the add-on won't touch it — add those two keys by hand.
- **ACME silently fails / no cert issued** → open the add-on **Log** tab (Settings → Add-ons → Traefik → Log) and look for `acme`. Common causes: token scope (see above), or Let's Encrypt rate-limit (5 certs/domain/hour) after a previous failure storm. Fix the cause, don't retry-loop.
- **Where are the logs?** Settings → Add-ons → Traefik → **Log** tab shows Traefik + the add-on backend output.

## Reachability sensors (bundled integration)

The addon ships a small HA integration that publishes `binary_sensor.traefik_route_<slug>_reachable` (one per Traefik service, `connectivity` device class) so automations can react to a backend going down.

- **Optional**: the sensors are a convenience, not required for routing. On first install a banner offers them ("Reachability sensors available"). A freshly-deployed integration isn't in HA's Add-Integration list until a Core restart, so click **Restart HA Core** first, then **Settings → Devices & services → Add Integration → Traefik** and accept the default URL. Don't want them? Click **Dismiss** (the banner stays gone until a newer integration version ships).
- **After an add-on update that changes the integration**: a separate "integration updated — restart to load" banner appears. Click **Restart HA Core** to pick up the new version.
- **Entities**: one binary_sensor per route (including the HA system route). `on` = all backends `UP` in Traefik's `serverStatus`; `off` = any `DOWN` (target unreachable); `unavailable` = Traefik not reachable, healthCheck hasn't completed its first cycle (~30s), OR the route no longer exists ("not configured"). `unavailable` deliberately covers "route removed" so a deleted route never reads as `off`/unreachable.
- **Add**: a new route on the Routes tab → its sensor appears on the next 30s poll.
- **Remove**: deleting a route does NOT delete its sensor (that would break dashboards/automations). The sensor goes `unavailable` and persists. To clean up a sensor you're truly done with, delete its device from Settings → Devices & services → Traefik.
- **Version coupling**: the integration ships *inside the addon image*. The "updated — restart" banner is keyed on the integration's actual content, not the add-on version — so an add-on release that doesn't change the integration won't ask you to restart. There is no separate update path.

## Security notes

- `/data/acme.json` is included in HA snapshots. The LE rate-limit on restore-reissue makes excluding it worse than the snapshot-leak risk for a LAN deployment. Encrypt your snapshots if this matters.
- basicAuth hashes are visible in the Traefik dashboard view of middleware definitions. One-way bcrypt — not reversible — but visible.
