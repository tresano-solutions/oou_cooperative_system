# Public website sign-in

The homepage Sign in link opens `/sign-in`. Visitors enter a cooperative code;
the page does not publish a tenant list, client examples or client portal links.
It uses the existing tenant resolver, then opens the validated HTTPS tenant
origin at `/login`. Passwords are never requested on the marketing site.

The page has no analytics scripts and does not persist the code in browser
storage. The resolver request carries the code as a query parameter, which may
appear in server access logs. Codes are not authentication secrets. Successful
lookup reveals the destination to that visitor; authorization still belongs to
the tenant application. This is not a mechanism for hiding DNS or making tenant
addresses secret.

## Deployment

In addition to deploying the new landing files, regenerate the Caddy config on
the VPS using `python3 generate.py` from `deploy/vps`, then validate and reload:

```sh
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
```

The generated rule proxies only `/api/mobile/v1/tenants/resolve` to the configured
marketing backend, normally HQ. No wildcard mobile API proxy is added. Existing
tenant records/environment mappings must be active and have their HTTPS
`cooperativems.com` subdomain configured. Custom tenant domains are deliberately
not accepted by this initial redirect allowlist.

Verify an authorized code reaches its login page, an unknown code remains on the
lookup page with an error, and browser Back returns to the neutral sign-in page.
