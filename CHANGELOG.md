# Changelog

## 0.1.0-alpha.24

- **The add-on is now confined by an enforcing AppArmor profile.** It previously
  ran with `apparmor: false`, which is not neutral: it means no AppArmor
  confinement at all, and the supervisor penalises the security rating for it.

  Every rule was traced to a logged denial rather than inferred. Three of them
  turned out to matter a great deal: Go's resolver reads `/etc/hosts`,
  `/etc/nsswitch.conf` and `/etc/resolv.conf` before any outbound connection,
  and `abstractions/base` does not cover them — so ACME and every backend health
  check depended on rules that were not present.

  Verified on a clean install with the data directory removed between passes:
  nine denials, then zero, then flipped to enforce and reinstalled. Traefik
  answers on `:80`, the renderer writes its config, and the bundled integration
  redeploys.

  **Not exercised:** the ACME/DNS-01 path that writes `/data/acme.json`. It needs
  Cloudflare credentials the test environment does not have, so the rules
  covering it remain the ones inferred when the profile was written.

## 0.1.0-alpha.23

- **New: cross-addon scaffold API.** Sibling Home Assistant add-ons
  can now POST to `http://traefik:8080/api/internal/routes` from
  inside the supervisor bridge network to scaffold a route into the
  Traefik draft. The route appears alongside any user-edited routes
  in the Routes tab; the user reviews + clicks Apply to publish.
  Bypasses the session and version gates (system call, not a user
  tab); authenticates with a non-empty `Bearer` token (homelab trust
  — the bridge network is internal-only).

  Body shape:
  ```json
  {
    "name": "davinci-resolve",
    "backend_kind": "external",
    "backend_host": "local_davinci-resolve",
    "backend_port": 5432,
    "scheme": "http",
    "tls": false,
    "source": "davinci-resolve"
  }
  ```

  Returns `{rid, name, message}` on success. 401 on missing auth
  header, 409 on duplicate hostname.

  First consumer is the DaVinci Resolve Postgres addon (its dashboard
  has a "Create scaffold in Traefik" button), but the API is generic.

## 0.1.0-alpha.22

- **Fixed: new routes no longer churn the table or the auto-save while
  you're filling them in.** Clicking **Add route** now opens a separate
  compose panel above the table; the new route only joins the grouped/
  sorted list when you click **Create** with the required fields
  (hostname, plus host + port for external backends) filled in. Two
  UX issues this resolves: the row no longer jumps between the
  "Home Assistant" and "External backend" groups as you change the
  backend kind, and there's no more stream of "route[N].hostname
  empty" / "external needs backend_host + backend_port" toasts from
  the half-typed state. Auto-save only fires once the route exists.
- **Fixed: auto-save error toasts deduplicated.** A regex- or
  validator-rejecting keystroke stream used to push a fresh sticky
  toast per debounce flush. Now there's one sticky toast per surface
  that updates in place and dismisses automatically when the next
  flush succeeds.

## 0.1.0-alpha.21

- **Per-field change indicators + undo (Routes + Configuration).** Every
  edited input lights up with an amber accent + a small undo icon to
  the right whenever its value differs from live. Click the undo icon
  to revert just that one field back to the live value (auto-save
  flushes the revert; pending count decrements accordingly). Wired up
  for: Routes hostname, backend (kind/host/port), scheme, TLS, Skip
  TLS verify; Configuration's Force SSL, log level, HTTP entry point,
  HTTPS entry point. Per-row "modified" amber dot from alpha.20 is
  unchanged and still summarises the row state.

## 0.1.0-alpha.20

- **Draft / live config split + Apply.** Editing the Routes, Middlewares,
  or Configuration tab no longer pushes changes to Traefik immediately.
  Every edit auto-saves to a **draft** (debounced; 500ms for routes /
  middlewares, 1500ms for Configuration's regex-validated fields). A
  sticky **"N pending changes — Apply / Discard all"** footer appears at
  the bottom of every tab whenever the draft differs from live. Click
  **Apply** to atomically commit draft → live and trigger Traefik
  render; click **Discard all** (with inline confirm) to reset the draft
  back to live. Per-row amber dot marks routes that differ from live.
- **Soft delete with Restore.** Clicking Remove on a route marks it as
  pending deletion — the row stays visible in the table with strike-
  through styling, sorted to the bottom of its group, with a
  **Restore** button to undo. Actual deletion happens on Apply.
- **Crash-safe Apply.** Apply uses a journal-based atomic commit: live
  files are staged as `*.applying` siblings, a journal marker is
  written, all three live files are atomically renamed, then the
  journal is deleted. If the addon is killed mid-Apply, the next boot's
  cont-init completes the pending rename and runs render. Render
  failures roll live back to the pre-Apply snapshot.
- **3-way merge on live drift.** When an addon migration mutates the
  live config behind the editor's back (e.g. a future migration adds
  a route or strips a removed field), pending draft edits are PRESERVED
  via a 3-way merge instead of being silently reset. Only fields the
  user AND the migration both edited surface as conflicts (with a
  banner explaining what was reconciled).
- **Version-skew guard.** Every mutating request now sends an
  `X-Addon-Version` header; the backend 409s if it doesn't match the
  running addon, prompting the user to reload after an upgrade rather
  than sending an old payload shape to a new validator.
- **`rid` (route uuid) + `mid` (middleware uuid).** Stable per-row
  identity assigned by a one-shot migration on first alpha.20 boot.
  Hidden from the UI; used internally so renames (hostname, middleware
  name) don't show as add+delete in the pending changes summary.

## 0.1.0-alpha.19

- **Click-to-sort columns in the Routes tab.** Click **Hostname**,
  **Backend**, **Scheme**, or **On** in the column header to sort by
  that field; click again to flip ascending/descending; click a third
  time to clear back to default (alphabetical hostname). Sorting
  applies within each group when grouped, or across the whole list
  when ungrouped. The HA system route stays pinned at the top of its
  group regardless of sort. Your sort choice persists across reloads
  and across "Group by" changes.
- **Tags removed.** The alpha.16 per-route tags feature has been
  removed entirely: no more tag chips on rows, no tag editor in the
  expanded panel, and the "Group by" dropdown is back to two options
  (External target / None). On upgrade, an idempotent migration strips
  any `tags:` arrays from `/data/routes.yml` so the next save round-trip
  stays clean -- no data loss for the rest of each route.

## 0.1.0-alpha.18

- **Proper icons in the Routes tab.** The emoji glyphs (`▶`/`▼` chevrons
  for group + row expand, `🔒` for TLS) have been replaced with crisp,
  monochrome MDI SVGs -- visually consistent with Home Assistant's own
  iconography and rendered at the correct text size. Vendored inline;
  no CDN.
- **"Skip TLS verify" is now a chip beside the lock.** Was a wide amber
  `skip-tls` text marker that wrapped narrow rows; is now a small
  amber shield-alert icon with a tooltip -- same information, fits on
  one line at any reasonable width.

## 0.1.0-alpha.17

- Fixed: the tag editor silently swallowed invalid input — typing a tag
  with disallowed characters (or pasting `foo,bar` expecting two tags)
  produced zero chips and no feedback. The editor now shows an inline
  red error explaining what was rejected and why, and `foo,bar` correctly
  becomes two tags.
- Fixed: the Home Assistant system route could get visually demoted
  below a freshly-added (still-unnamed) user route when the group sorted
  alphabetically by hostname. System rows now pin to the top of their
  group regardless of name.
- Changed: external routes with no host set group under "External
  backend (no host set)" instead of the terse "(no host)".

## 0.1.0-alpha.16

- **Routes tab redesigned.** Every route now renders as a one-line compact
  row by default — hostname, backend, scheme, tags, actions. Click the
  chevron on the left of a row to expand the full editor inline; click
  again (or open another row) to collapse it. New routes from "Add route"
  open expanded by default.
- **Routes are grouped.** A "Group by" dropdown next to "Add route"
  controls the layout. Default: **External target** — routes pointing
  at the same backend host (port ignored) collapse into one group; the
  HA self-route gets its own pinned "Home Assistant" group at the top.
  Pick a tag to group by it, or "None" for a flat list. Your selection
  + which groups you've collapsed both persist in the browser across
  reloads.
- **Free-form tags per route.** Each route can carry any number of
  organisational tags (e.g. `proxmox`, `lan-only`, `prod.web`). Tags are
  edited via a chip-list in the expanded panel and surface as chips in
  the compact row (max 3 visible + "+N more"). Tags drive the "Group by"
  dropdown — define a tag on a route and it immediately becomes an
  option. They are purely organisational; Traefik never sees them.
- **System row UX normalised.** The "System" pill now sits in the
  actions column (right side) where Remove lives for user routes —
  system rows can't be deleted, so the slot is free. The leftmost cell
  is now the expand chevron, consistent with user rows.
- Internal: backend gains an optional `tags: list[str]` field per route
  with content rules `^[A-Za-z0-9._ -]+$` and ≤32 chars; migration
  backfills `tags: []` on every existing route. `tags` is NOT in the
  system-route locked set, so you can tag the HA self-route for your own
  grouping.

## 0.1.0-alpha.15

- Fixed: every Save on the Routes tab failed with "system route field
  'skip_tls_verify' is locked" — the alpha.14 per-route bool was being
  edited correctly but dropped from the save payload, which then mismatched
  what's on disk. Save round-trips cleanly now.
- Fixed: "Discard changes" on the Routes tab popped up a confusing
  "Another session is editing — opened 2s ago" prompt against your own
  session. The discard now just re-fetches routes (matching the Middlewares
  tab pattern); separately, the server's claim endpoint now recognizes a
  matching session header and returns a no-op refresh instead of 409.
- Fixed: after another tab took over your editor session, the persistent
  "Session was taken over" toast covered the read-only banner's "Take over"
  button on narrow viewports, making it un-clickable. The toast now slides
  down when the read-only banner is visible.
- Changed: the HA system route's middlewares list no longer shows the
  `redirect-to-https` chip while Force SSL is on. Force SSL handles the
  redirect at the entry-point level — showing the per-route chip implied a
  different mechanism than what actually runs.
- Changed: the Force SSL help text was rewritten to drop a confusing
  parenthetical about middleware visibility.
- Internal: Tailwind 3.4.17 moved from build-time CDN fetch to a vendored
  file in the repo. Removes a build-time network dependency and protects
  against the Play CDN (in maintenance since Tailwind v4) going away.
  The script's runtime production-mode console warning is unchanged —
  Tailwind itself emits it regardless of where the bundle was loaded from;
  silencing it requires migrating off the Play CDN model (CLI or v4), out
  of scope here. (Alpine.js still build-time-fetched; its CDN is healthy.)

## 0.1.0-alpha.14

- Changed: **Skip TLS verify** is now a per-route property, not a middleware.
  The checkbox under each route's scheme (visible when scheme is `https`)
  is the only place that controls it. The old "Built-in" `skip-tls-verify`
  row has been removed from the Middlewares tab — there was nothing to
  configure there. Existing routes are migrated automatically on first boot;
  no action required.
- Changed: the `redirect-to-https` "Built-in" row no longer appears on the
  Middlewares tab either. Its two parameters (`scheme: https`, `permanent:
  true`) aren't user-knobs in practice — the add-on sets the canonical
  defaults — and it's still toggled per route via the route middlewares
  dropdown (and superseded globally by **Force SSL**).
- Fixed: the browser used to keep the previous release's `app.js` cached
  across updates, so the UI looked broken until you hard-refreshed.
  Each release now busts the cache automatically.
- Internal: removed the "system middleware" / "synthetic middleware-type"
  scaffolding that backed the previous skip-tls-verify model. The route
  schema's new `skip_tls_verify: bool` field is the single source of truth;
  the renderer reads it directly.

## 0.1.0-alpha.13

- Fixed: deleting a route's device from Home Assistant could leave the
  sensor missing until next HA restart, even if the route was still live.
  The integration now drops its internal record on device delete so the
  sensor re-materialises on the next 30 s coordinator poll.
- Fixed: a narrow race during add-on startup could log
  "duplicate unique_id" warnings for reachability sensors. The setup path
  now records known routes before wiring the entity-add callback.
- Improved: the Repairs fix-flow now dispatches on `issue_id` (futureproofs
  for additional issue kinds), and the coordinator skips a per-poll
  executor dispatch for the already-snapshotted loaded content hash.

## 0.1.0-alpha.12

- New: unified notification system. Success and info messages auto-disappear
  after a few seconds; errors stay until you dismiss them. Replaces the six
  ad-hoc inline error/success panels.
- New: concurrent-edit protection. If two tabs or browsers try to edit at the
  same time, the second sees a "Take over | View read-only" prompt. Sessions
  auto-expire after 60 s of inactivity.
- New: "Couldn't load …" panel per section. If a section's data fails to
  load, the form is hidden and Save is disabled until you reload — preventing
  a stale load from being "fixed" by clicking Save and overwriting the real
  config with an empty one. (This used to be a silent one-click data-loss
  path on the Middlewares tab.)
- New: composite "Home Assistant Core restart needed" banner that lists each
  pending reason as a bullet. Replaces the previously-stacked three banners
  (integration update, trusted-proxies fix, integration available).
- New: full keyboard + screen-reader support on the setup wizard and the
  route-middlewares dropdown. The wizard traps focus, closes on Escape,
  and announces itself as a dialog; the multiselect is a proper ARIA
  combobox (Arrow keys navigate, Space/Enter toggle, Escape closes).
- New: inline form validation for entry-point names, Cloudflare token,
  ACME email, and domain. Save is disabled until visible errors clear.
- New: routes table now scrolls horizontally on narrow viewports (HA
  Companion-friendly) instead of overflowing the page.
- Improved: the middleware type-change confirmation is now an inline strip
  inside the card instead of a native browser alert that blocks the page.
- Improved: status dots and icon-only buttons now announce their meaning
  to screen readers.
- Improved: when the add-on backend can't be reached for 15 s, route status
  dots fade to "unknown" (rather than lying green) and a sticky
  "backend unreachable" notification appears.

## 0.1.0-alpha.11

- Fixed: a crash or kernel-level interruption mid-save could leave the add-on's
  stored config corrupted and prevent the add-on from booting. Writes are now
  fsync'd before the rename, on both `/data/*.yml` and the rendered
  `/etc/traefik/*.yml` files.
- Fixed: a save with an invalid route used to persist the bad config to disk
  before reporting the error, so subsequent restarts would also fail. Saves
  now roll back to the prior content if the render step fails.
- Fixed: a corrupt `/data/config.yml` could be silently replaced with defaults
  on the next save, losing stored markers. The add-on now refuses to save and
  reports the parse error so you can fix the file by hand.
- Fixed: backend errors used to return Python tracebacks in plain text;
  errors now return JSON the UI can render cleanly. Tracebacks still go to the
  add-on log for debugging.
- Fixed: pasting a multi-line Cloudflare API token used to silently break
  Cloudflare authentication. The token is now validated for shape on save.
- Fixed: clicking "Fix automatically" twice for trusted_proxies would
  overwrite the original `configuration.yaml.traefik-addon.bak`. The add-on
  now refuses a second fix when an earlier backup is already present.

## 0.1.0-alpha.10

- Routes tab: each route now shows a **status dot** to the left of its hostname —
  green = backend reachable, red = unreachable, grey = unknown (Traefik down,
  health check still pending, or route disabled). Refreshes every few seconds.

## 0.1.0-alpha.9

- Fixed: new routes now show up as reachability sensors automatically (within
  ~30s), without needing a Home Assistant restart. The integration's poller
  could stop when no sensors were live; it now always polls.
- New: **Force SSL** setting on the Configuration page. When enabled, every HTTP
  request is redirected to HTTPS globally (and the built-in redirect middleware
  is applied for you and hidden). HTTP-only routes are not served while it's on.
- Routes tab: middlewares are now a compact dropdown (shorter rows), and an
  HTTPS route shows a **Skip TLS verify** checkbox (for self-signed backends)
  right under the scheme — no need to attach the middleware by hand.

## 0.1.0-alpha.8

- Fixed: the built-in middlewares' **Type** field showed the wrong value
  (`basicAuth`) in the Middlewares tab. It now correctly shows the locked type
  (`redirectScheme` / `skipTlsVerify`).

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
