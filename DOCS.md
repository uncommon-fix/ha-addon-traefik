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

- **Edits auto-save into a draft; Apply commits to Traefik (alpha.20).** Every change to Routes, Middlewares, or Configuration auto-saves to a draft after a short debounce (500ms for routes/middlewares, 1500ms for the regex-validated Configuration fields). A sticky **"N pending changes — Apply / Discard all"** footer at the bottom of the page appears whenever the draft differs from live. Click **Apply** to atomically commit the draft to Traefik's live config + render; click **Discard all** (inline confirm) to reset back to live. Per-row amber dot marks routes that differ from live; deleted routes stay visible with strikethrough until Apply (click **Restore** to undo before applying).
- **Routes tab — compact rows + grouping.** Each route renders as a single line by default; click the chevron on the left to expand the editor inline. The "Group by" dropdown next to **Add route** groups the list (default: by backend target, so e.g. all routes pointing at `10.0.0.20` collapse into one group). Pick **None** for a flat list. Your selection — and which groups you've collapsed — persists in the browser across reloads.
- **Routes tab — column sort (alpha.19).** Click a column header (Hostname / Backend / Scheme / On) to sort. Clicking again toggles ascending → descending → clear. Sorting applies within each group when grouped, and across the whole list when ungrouped. The HA system route stays pinned to the top of its group regardless of sort. Your sort sticks across reloads and across grouping changes.
- **Routes tab — system row.** The HA system route is pinned at the top in the "Home Assistant" group. The "System" badge sits in the actions column (right side); the row has no Remove button. Only its hostname is editable — backend, scheme, TLS, middlewares, and Skip-TLS are add-on-managed and locked.
- **Routes tab — middlewares.** Pick middlewares per route from the dropdown in the expanded editor (your configured middlewares only; add-on-managed ones like `redirect-to-https` when Force SSL is on are hidden — they're applied elsewhere). Order = the order you tick them.
- **Force SSL (Configuration page).** When enabled, every HTTP request is redirected to HTTPS at the entry point (301), globally. While on, an HTTP-only route (TLS off) is **not served** (it would redirect to an HTTPS router that doesn't exist). Leave it off to redirect only the routes you attach `redirect-to-https` to (via the route's middlewares dropdown).
- **Middlewares tab.** Lists only the middlewares you create (basicAuth, ipAllowList, headers, redirectScheme). Add-on-managed ones (`redirect-to-https`) are not shown — they're toggled per route via the dropdown, or globally via Force SSL on the Configuration page.
- **Fronting an HTTPS backend with a self-signed cert.** Set the route's scheme to `https`, then tick the **Skip TLS verify** checkbox that appears under the scheme — Traefik will skip verifying that backend's certificate. (Per-route property, not a middleware. LAN-appropriate; affects that route's backend only.)
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

## Editor sessions (one writer at a time)

The add-on UI lets one tab/browser hold the **edit session** at a time so two
people (or two of your own tabs) can't silently clobber each other's saves.

- **Opening the UI** claims the session automatically. You see "Read-only"
  state only when something else is already editing.
- **If another session is active**, you'll see a prompt: **Take over** (the
  other session is then disabled — its next save shows "Your session was
  taken over" with a Reload button) or **View read-only** (you can navigate
  and inspect, but every Save/Restart/Fix button is disabled).
- **Sessions expire after 60 s of inactivity** (no polling = no heartbeat).
  Reload the page to claim a new one.

## Notifications + load failures

- **Success and info notifications** appear top-right and auto-fade after a
  few seconds. **Error notifications** stay until you dismiss them.
- **If a section can't load** (network glitch, backend restart mid-poll), you
  see a "Couldn't load …" panel with a **Reload** button INSTEAD of the form,
  and the Save button is disabled. This prevents a stale-load → empty-save →
  silent-clobber loop.
- **Form validation** runs as you type (entry-point names, Cloudflare token
  shape, email, domain). Save is disabled until visible errors clear.

## Security notes

- `/data/acme.json` is included in HA snapshots. The LE rate-limit on restore-reissue makes excluding it worse than the snapshot-leak risk for a LAN deployment. Encrypt your snapshots if this matters.
- basicAuth hashes are visible in the Traefik dashboard view of middleware definitions. One-way bcrypt — not reversible — but visible.
