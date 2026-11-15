# HA Storage Optimizer

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

See **where your Home Assistant disk space went** and free it safely. Home Assistant tells you the disk is filling up; this app shows which area, which app and which folder is using it, and cleans the parts that are safe to clean.

## Features

- **Overview:** free, used and total space, with a 30-day chart of free space and a low-space warning.
- **Where the space is:** the eight areas Home Assistant reports (system, app data, app configuration, media, share, backups, SSL, Home Assistant), then app data per app and the configuration folder per folder.
- **Recorder database:** **Purge**, or **Purge and repack** to shrink the file. The app watches the database size afterwards and reports what was really freed.
- **Backups:** pick local backups that are not password-protected and delete them.
- **Logs and caches:** empty Home Assistant's log file and delete its text-to-speech cache. Shown only when there is something to clean.
- **Nothing is automatic unless you switch it on.** A daily clean-up is off by default, and even with it on, backups are never deleted unless you switch that on separately.
- **A short, fixed list of what it may touch.** The operating system, Docker, other apps' data, `.storage`, custom integrations and the database files themselves are never changed.

## Install

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

1. Add the repository above.
2. Install **HA Storage Optimizer** and start it.
3. Open **Storage Optimizer** from the sidebar. Only administrators see it.

Full documentation is in the app's **Documentation** tab, or [DOCS.md](DOCS.md). See the [changelog](CHANGELOG.md) for release history.
