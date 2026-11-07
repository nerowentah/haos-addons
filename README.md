# HAOS Apps by nerowentah

Home Assistant OS apps (formerly called add-ons) covering:
- LG webOS Developer Mode management
- Malaysian utility checkers
- a manager for your Companion app devices

## Add this repository

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

Or add it by hand: go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:

```
https://github.com/nerowentah/haos-addons
```

## Apps

| | App | Version | Arch | What it does |
| --- | --- | --- | --- | --- |
| <img src="tnb-usage-checker/icon.png" width="32" alt=""> | [TNB Usage Checker](tnb-usage-checker/) | 0.3.1 | aarch64, amd64 | Imports myTNB usage and cost into the Energy dashboard and estimates your TNB bill live, for multiple accounts under one myTNB login. |
| <img src="lg-webos-devmode-mgmt/icon.png" width="32" alt=""> | [LG webOS DevMode Mgmt](lg-webos-devmode-mgmt/) | 1.7.2 | aarch64, amd64 | Renews LG webOS Developer Mode sessions on a schedule so they never expire. |
| <img src="mobile-device-manager/icon.png" width="32" alt=""> | [Mobile Device Manager](mobile-device-manager/) | 0.1.1 | aarch64, amd64 | Lists your Companion app devices with last seen, battery and every automation, script and notify call that uses them. Flags stale and duplicate devices and removes them safely. |

**Planned:** TM Unifi Bill Checker (`tm-unifi-bill-checker/`) and Ranhill SAJ Bill Checker (`ranhill-saj-bill-checker/`).

Each app folder contains:
- a short `README.md` (the store's Info tab)
- a full `DOCS.md` (the app's Documentation tab)
- a `CHANGELOG.md`

## What every app has in common

- **A sidebar panel** opened through ingress. It follows your Home Assistant theme, including dark mode, and works on phones. Panels can link straight to the Home Assistant page you need to edit.
- **Switches that apply immediately.** They are saved to the app options. Edits made in the Configuration tab are picked up within about 30 seconds, with no restart.
- **No crash loops.** Invalid options start a setup page in the panel that names the problem. The app starts by itself once the options are fixed.
- **Runs as an s6-overlay service** (`rootfs/etc/s6-overlay/s6-rc.d/<app>/`), with quiet s6 logging.
- **Failure notifications** go to the Companion apps linked to your Home Assistant. Phones are found automatically, and each has its own switch in the panel.
- **Privacy.** Passwords, tokens and account-specific URLs are never logged and never shown in the panel.

## Repository layout

```
repository.json            store metadata
tnb-usage-checker/         one folder per app: config.yaml, Dockerfile, docs, icons, and
                           rootfs/app/ (code + panel) + rootfs/etc/s6-overlay/ (the s6 service)
lg-webos-devmode-mgmt/
mobile-device-manager/
.github/workflows/         builder.yaml (finds changed apps), build-app.yaml (builds, signs, verifies one app),
                           retry-signing.yaml (re-runs builds that failed only because Sigstore was down)
pyproject.toml             ruff settings for every app
```

- **Naming:** folder names are brand or product first, in kebab-case (`<brand>-<product>`), for example `tnb-usage-checker`.
  - The store name matches the folder name.
  - The `slug` and `image` never change after release, because changing them would make Home Assistant treat the app as new.
- **Folder depth:** keep apps exactly one level below the root. The CI app finder only looks two levels deep.
- **Shared panel files:** `webui.py` and `web/` (`index.html`, `panel.css`, `panel.js`) are **identical copies** in every app, because each image is built from its own folder only. Edit one copy, copy it to the others, then check with `cmp`.

## Development

```bash
ruff check . && ruff format --check .
python -m unittest discover -s tnb-usage-checker/tests       # needs paho-mqtt and websocket-client
python -m unittest discover -s mobile-device-manager/tests
```

**Pull requests:** CI builds every changed app for aarch64 and amd64.

**Merges to `main`:**
- CI pushes the images to `ghcr.io/nerowentah/<arch>-<image>` and publishes a multi-arch manifest, tagged with the version and `latest`.
- Every image and manifest is **signed with Sigstore cosign**, keyless through GitHub OIDC. The workflow then **verifies the signature** before it finishes.
- If Sigstore is down, `retry-signing.yaml` waits for it to recover and re-runs the failed jobs. Images are never published unsigned.

**Releasing a change:**
1. Bump `version` in the app's `config.yaml` and add a `CHANGELOG.md` entry.
2. Home Assistant offers the update once the new image is published.

**Checking an image signature yourself:**

```bash
cosign verify ghcr.io/nerowentah/power-tnb:0.3.1 \
  --certificate-identity-regexp '^https://github.com/nerowentah/haos-addons/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Disclaimer

- These are community projects. They are not affiliated with LG Electronics, Tenaga Nasional Berhad, TM or Ranhill SAJ.
- The myTNB portal has no public API. TNB Usage Checker uses it unofficially, so it may break if TNB changes its website.
