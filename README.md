# HAOS Apps by nerowentah

Home Assistant OS apps (formerly called add-ons): an LG webOS Developer Mode session renewer and Malaysian utility trackers.

## Add this repository

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

Or add it by hand: go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:

```
https://github.com/nerowentah/haos-addons
```

## Apps

| | App | Version | Arch | What it does |
| --- | --- | --- | --- | --- |
| <img src="lg-webos-auto-renew/icon.png" width="32" alt=""> | [LG webOS Dev Session Auto Renew](lg-webos-auto-renew/) | 1.7.0 | aarch64, amd64 | Renews LG webOS Developer Mode sessions on a schedule so they never expire. |
| <img src="utilities-malaysia-power-tnb/icon.png" width="32" alt=""> | [Power TNB](utilities-malaysia-power-tnb/) | 0.2.0 | aarch64, amd64 | Imports myTNB usage and cost into the Energy dashboard and estimates your TNB bill live, for multiple accounts. |

**Planned:** TM Unifi (network) and Ranhill SAJ (water) for Malaysia.

Each app folder has a short `README.md` (the store's Info tab), a full `DOCS.md` (the app's Documentation tab) and a `CHANGELOG.md`.

## What every app has in common

- **A sidebar panel** opened through ingress. It follows your Home Assistant theme, including dark mode, and works on phones.
- **Switches that apply immediately.** They are saved to the app options. Edits made in the Configuration tab are picked up within about 30 seconds, with no restart.
- **Notifications** go to the Companion apps linked to your Home Assistant. Phones are found automatically, and each has its own switch in the panel.
- **Privacy.** Passwords, tokens and account-specific URLs are never logged and never shown in the panel.

## Repository layout

```
repository.json                    store metadata
lg-webos-auto-renew/               one folder per app (config.yaml, Dockerfile, rootfs/, docs)
utilities-malaysia-power-tnb/      category folded into the folder name: <category>-<country>-<kind>-<provider>
.github/workflows/                 builder.yaml (finds changed apps) + build-app.yaml (builds one app)
pyproject.toml                     ruff settings for every app
```

- **Folder depth:** keep apps exactly one level below the root. The CI app finder only looks two levels deep.
- **Shared panel files:** `webui.py` and `web/` (`index.html`, `panel.css`, `panel.js`) are **identical copies** in every app, because each image is built from its own folder only. Edit one copy, copy it to the others, then check with `cmp`.

## Development

```bash
ruff check . && ruff format --check .
python -m unittest discover -s utilities-malaysia-power-tnb/tests   # needs paho-mqtt and websocket-client
```

**Pull requests:** CI builds every changed app for aarch64 and amd64.

**Merges to `main`:** CI pushes signed images to `ghcr.io/nerowentah/<arch>-<image>` and publishes a multi-arch manifest, tagged with the version and `latest`.

**Releasing a change:**
1. Bump `version` in the app's `config.yaml` and add a `CHANGELOG.md` entry.
2. Home Assistant offers the update once the new image is published.

## Disclaimer

- These are community projects. They are not affiliated with LG Electronics, Tenaga Nasional Berhad, TM or Ranhill SAJ.
- The myTNB portal has no public API. Power TNB uses it unofficially, so it may break if TNB changes its website.
