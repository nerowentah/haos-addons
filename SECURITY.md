# Security

## Reporting a problem

Please report a vulnerability privately: open a
[security advisory](https://github.com/nerowentah/haos-addons/security/advisories/new)
instead of a public issue. Include the app, its version and the steps to
reproduce. Leave out real tokens, passwords, account numbers and hostnames.

## What the apps are trusted with

| What | Which apps |
| --- | --- |
| Supervisor token (Home Assistant and Supervisor API) | all |
| Home Assistant `/config`, read-only (`.storage/auth`) | API Key Manager |
| Home Assistant `/config`, read-write (`configuration.yaml`) | Cloudflare Tunnel, SSL Cert Manager |
| `/ssl`, read-write (certificates and private keys) | Cloudflare Tunnel, SSL Cert Manager |
| Cloudflare tunnel token or API token | Cloudflare Tunnel, SSL Cert Manager |
| Portal sign-in details and session tokens | TNB Usage Checker, LG webOS DevMode Mgmt |

## Trust boundaries

1. **Browser to panel.** Every app serves its panel on port 8099 inside the
   container. Only the Supervisor ingress proxy may connect; Home Assistant has
   already signed the user in, and the panel is limited to administrators.
   Everything the page sends is treated as untrusted input.
2. **App to Supervisor and Home Assistant.** Calls go to `http://supervisor`
   on the internal network with the Supervisor token. The token is never
   logged or written to disk.
3. **App to the Internet.** Outbound calls use HTTPS with certificate
   verification. Pages and API replies from Cloudflare, Let's Encrypt and the
   utility portals are parsed as untrusted input.
4. **App to disk.** State lives in `/data`. Files with secrets are written
   with owner-only permissions and replaced atomically. Secrets given as
   `!secret` references stay references in the stored options.
5. **External programs.** `cloudflared`, `certbot` and `nginx` are started
   with an argument list built by the app (never through a shell); option
   values that reach them are validated against an allow-list first.

## Supply chain

- Images are built by GitHub Actions from this repository and signed with
  Sigstore (keyless). Verify one with:

  ```sh
  cosign verify ghcr.io/nerowentah/<image>:<version> \
    --certificate-identity-regexp '^https://github.com/nerowentah/haos-addons/' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
  ```

- The base image is pinned by digest and every GitHub Action by commit;
  Dependabot proposes the updates.
- `cloudflared` is downloaded at build time and checked against a pinned
  SHA-256 digest. Everything else comes from Alpine packages; no `pip install`
  happens in an image.
- CI runs ruff (including the flake8-bandit rules), every app's tests and
  CodeQL on each pull request.
