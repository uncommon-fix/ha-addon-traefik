"""The DNS-01 providers this add-on offers, and the credentials each needs.

ONE source of truth, imported by backend/server.py (validation + the UI's field
list), render.py (deciding whether ACME can run) and cont-init (exporting the
variables Traefik's lego needs). Adding a provider is one entry here.

WHY A CURATED LIST, when Traefik supports 218
---------------------------------------------
`research/16-traefik-dns-providers.{md,json}` in the workspace has all of them,
gathered by a research workflow. That data is a useful starting point and it is
NOT trustworthy per-row: every entry claimed "confirmed" while the verification
pass proved three of them wrong, and two of those corrections did not propagate
(`gcloud` still lists an optional variable as required; `acmedns` has an English
sentence where a variable name should be).

That matters more than it looks. A wrong variable name does not fail when the
user saves it. It fails when a certificate is issued or renewed -- weeks later,
quietly. So the providers here are the ones whose variables were checked against
lego's own documentation by hand, and nothing is offered that was not.

Every name below is verbatim from https://go-acme.github.io/lego/dns/<code>/.
If you add one, fetch that page and read the Credentials table. Do NOT derive a
variable name from the provider code: lego's `ibmcloud` uses SOFTLAYER_*, which
is exactly the assumption that would break silently.

FIELD SHAPES
------------
1 to 3 variables covers everything here. Providers with mutually exclusive auth
modes (route53's credential chain, ovh's three sets, cloudflare's legacy
key+email pair) are represented by their PRIMARY documented set only -- the
add-on has no UI for mode switching, and pretending otherwise would produce a
form that cannot express what the user has.
"""

from __future__ import annotations

# Not an ACME provider: no certificate authority is involved and Traefik serves
# its own self-signed certificate. Handled specially everywhere.
PROVIDER_LOCAL = "local"

# The default, and the only provider that existed before this table.
PROVIDER_DEFAULT = "cloudflare"


def _f(env: str, label: str, *, secret: bool = True, help: str = "") -> dict:
    """One credential field. `secret` drives masking in the UI and whether the
    value is ever sent back to the browser (it is not)."""
    return {"env": env, "label": label, "secret": secret, "help": help}


# code -> definition. `fields` are ALL required; optional tuning variables that
# lego also accepts (timeouts, TTLs) are deliberately not exposed.
PROVIDERS: dict[str, dict] = {
    "cloudflare": {
        "name": "Cloudflare",
        "fields": [
            _f("CF_DNS_API_TOKEN", "API token",
               help="Needs Zone:Zone:Read and Zone:DNS:Edit on the target zone."),
        ],
    },
    "desec": {
        "name": "deSEC.io",
        "fields": [_f("DESEC_TOKEN", "API token")],
    },
    "digitalocean": {
        "name": "DigitalOcean",
        "fields": [_f("DO_AUTH_TOKEN", "API token")],
    },
    "duckdns": {
        "name": "Duck DNS",
        "fields": [_f("DUCKDNS_TOKEN", "Token",
                      help="The token from your Duck DNS account page.")],
    },
    "gandiv5": {
        "name": "Gandi Live DNS",
        "fields": [_f("GANDIV5_PERSONAL_ACCESS_TOKEN", "Personal access token",
                      help="The older API key is deprecated by Gandi.")],
    },
    "hetzner": {
        "name": "Hetzner DNS",
        "fields": [_f("HETZNER_API_TOKEN", "API token")],
    },
    "ionos": {
        "name": "IONOS",
        "fields": [_f("IONOS_API_KEY", "API key",
                      help="The combined form, prefix.secret, exactly as IONOS shows it.")],
    },
    "namecheap": {
        "name": "Namecheap",
        "fields": [
            _f("NAMECHEAP_API_USER", "API user", secret=False),
            _f("NAMECHEAP_API_KEY", "API key"),
        ],
    },
    "netcup": {
        "name": "netcup",
        "fields": [
            _f("NETCUP_CUSTOMER_NUMBER", "Customer number", secret=False),
            _f("NETCUP_API_KEY", "API key"),
            _f("NETCUP_API_PASSWORD", "API password"),
        ],
    },
    "porkbun": {
        "name": "Porkbun",
        "fields": [
            _f("PORKBUN_API_KEY", "API key"),
            _f("PORKBUN_SECRET_API_KEY", "Secret API key"),
        ],
    },
    "route53": {
        "name": "Amazon Route 53",
        "fields": [
            _f("AWS_ACCESS_KEY_ID", "Access key ID", secret=False),
            _f("AWS_SECRET_ACCESS_KEY", "Secret access key"),
        ],
        # route53 can also authenticate from an instance role or a shared
        # credentials file. Neither exists in an add-on container, so the
        # explicit key pair is the only honest option to offer.
    },
}

for _code, _p in PROVIDERS.items():
    _p.setdefault("docs_url", f"https://go-acme.github.io/lego/dns/{_code}/")

# Every environment variable this add-on may ever write, across all providers.
# cont-init clears the ones not in use, so switching provider cannot leave a
# stale credential in the container environment for lego to pick up.
ALL_CREDENTIAL_ENV: set[str] = {
    f["env"] for p in PROVIDERS.values() for f in p["fields"]
}

# Accepted values of config.yml's `provider`.
ALLOWED_PROVIDERS: set[str] = set(PROVIDERS) | {PROVIDER_LOCAL}


def required_env(provider: str) -> list[str]:
    """Variable names that must be non-empty for this provider to issue certs.
    Empty for `local` and for anything unknown, which both mean "no ACME"."""
    p = PROVIDERS.get((provider or "").strip().lower())
    return [f["env"] for f in p["fields"]] if p else []


def ui_catalog() -> list[dict]:
    """What /api/providers returns. Field definitions only -- never values."""
    out = [{
        "code": PROVIDER_LOCAL,
        "name": "Local — self-signed, no account needed",
        "fields": [],
        "docs_url": "",
        "acme": False,
    }]
    for code, p in sorted(PROVIDERS.items(), key=lambda kv: kv[1]["name"].lower()):
        out.append({
            "code": code,
            "name": p["name"],
            "fields": p["fields"],
            "docs_url": p["docs_url"],
            "acme": True,
        })
    return out
