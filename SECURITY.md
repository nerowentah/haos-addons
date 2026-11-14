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
- The published `:latest` images are scanned with Trivy every week
  (`.github/workflows/scan-images.yaml`): the Alpine packages, and the Python
  modules those packages install, matched against the PyPI advisories. The
  results appear under code scanning next to CodeQL's.

## Known advisories

SSL Cert Manager installs Alpine 3.24's `py3-cryptography`, currently 47.0.0.
Four advisories name that version. None of them can be reached in the image:

| Advisory | What it is about | Why it does not apply |
| --- | --- | --- |
| GHSA-537c-gmf6-5ccf | The OpenSSL copy built into the PyPI wheels | Alpine's package uses the system OpenSSL, which `apk` keeps patched; no wheel is installed. |
| CVE-2026-69247 | Error and timing oracle in PKCS#7 EnvelopedData decryption | Nothing in the image decrypts PKCS#7. |
| CVE-2026-69248 | Wildcard names escape name constraints in the X.509 path verifier (`cryptography.x509.verification`) | The app does not use that verifier: it parses certificates and checks single signatures. TLS connections are verified by Python's `ssl` module with OpenSSL. |
| CVE-2026-69249 | Slow rejection of chains with duplicate self-signed certificates, in the same verifier | Same as above. |

The Alpine packages are not pinned to a version, so a rebuild picks up
Alpine's fixed package as soon as it is published. Until then the four ids are
listed in `.github/trivy/trivyignore.yaml`, limited to version 47.0.0 and with
an expiry date, so the weekly scan reports them again if either changes. CI
itself tests against a fixed release (`requirements-dev.txt`).
