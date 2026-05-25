# Traefik — Home Assistant add-on

LAN reverse-proxy for a Home Assistant OS install. Bundles Traefik 3.7.1,
terminates TLS via Cloudflare DNS-01 ACME, and provides a custom UI under HA
ingress for routes, middlewares, and the Traefik dashboard — plus a bundled
integration that publishes a reachability `binary_sensor` per route.

> [!WARNING]
> Early **public alpha** — expect breaking changes. Bug reports welcome in
> [Issues](https://github.com/uncommon-fix/ha-addon-traefik/issues).

## Install

This add-on is distributed through the **uncommon-fix add-on index**. In Home
Assistant: Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
`https://github.com/uncommon-fix/ha-addons`, then install **Traefik** from the
store.

See [`DOCS.md`](DOCS.md) for first-run, configuration, and troubleshooting.
