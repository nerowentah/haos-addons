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
- Every build runs `apk upgrade`, so an image carries the security fixes
  Alpine published after the base image was built. The base image's `tempio`
  binary is removed: no app uses it.
- CI runs ruff (including the flake8-bandit rules), every app's tests and
  CodeQL on each pull request.
- The published `:latest` images are scanned with Trivy every week and after
  each publish (`.github/workflows/scan-images.yaml`): the Alpine packages, the
  bundled programs, and the Python modules the Alpine packages install,
  matched against the PyPI advisories. The results appear under code scanning
  next to CodeQL's.

## Known advisories

Some findings have no fixed version to install yet: Alpine 3.24 has not
updated the package, or the fix waits for the next `cloudflared` release. Each
one below was checked against what the app does. They are listed in
`.github/trivy/trivyignore.yaml`, limited to the exact version and with an
expiry date, so the scan reports them again when either changes. The Alpine
packages are not pinned to a version: a rebuild picks up a fixed package as
soon as Alpine publishes it.

### SSL Cert Manager: Python modules from Alpine

| Module | Advisory | What it is about | Why it does not apply |
| --- | --- | --- | --- |
| cryptography 47.0.0 | GHSA-537c-gmf6-5ccf | The OpenSSL copy built into the PyPI wheels | Alpine's package uses the system OpenSSL, which `apk` keeps patched; no wheel is installed. |
| | CVE-2026-69247 | Error and timing oracle in PKCS#7 EnvelopedData decryption | Nothing in the image decrypts PKCS#7. |
| | CVE-2026-69248 | Wildcard names escape name constraints in the X.509 path verifier (`cryptography.x509.verification`) | The app does not use that verifier: it parses certificates and checks single signatures. TLS connections are verified by Python's `ssl` module with OpenSSL. |
| | CVE-2026-69249 | Slow rejection of chains with duplicate self-signed certificates, in the same verifier | Same as above. |
| urllib3 2.7.0 | CVE-2026-97687 | TLS settings for an HTTPS proxy and for the target server get mixed up | No proxy is configured. |
| | CVE-2026-97688, CVE-2026-97689 | A malicious server can stall a streamed response or make it use unbounded memory | Only certbot uses urllib3, and only to reach Let's Encrypt and the Cloudflare API over verified TLS. The worst case is a failed renewal run. |
| anyio 4.12.1 | CVE-2026-63374 | Host name check for internationalized names in `connect_tcp()` and `TLSStream.wrap()` | anyio is installed only because httpx depends on it (Cloudflare library of certbot's DNS plugin). That library uses httpx's synchronous client, which does not go through anyio. |
| | CVE-2026-64847 | Process-pool workers can block on a full pipe | Nothing starts an anyio process pool. |
| setuptools 82.0.1 | CVE-2026-59890, GHSA-xmj3-9gf7-h52w | Handling of packages while building or installing them | Nothing is built or installed in the running image. |

Alpine's anyio package reports its version as `0.0.0`; the scan therefore
lists it under that version.

CI itself tests against a fixed cryptography release (`requirements-dev.txt`).

### Cloudflare Tunnel: libraries inside `cloudflared` 2026.11.0

| Library | Advisory | Why it is accepted |
| --- | --- | --- |
| golang.org/x/net 0.61.0 | CVE-2026-78661, CVE-2026-97045 | 2026.11.0 is the latest `cloudflared` release; the libraries are compiled into Cloudflare's binary and are updated with it. The connector only talks to Cloudflare's edge and to the local Home Assistant. |
| golang.org/x/crypto 0.58.0 | GO-2026-5932 | Same as above. |
| go.opentelemetry.io/otel 1.45.0 | CVE-2026-128085 | Same as above. |

These entries stop matching as soon as `cloudflared` is updated.
