# Changelog

## 0.1.9

- **Shared panel update (security):** the panel only accepts changes that come from its own page (a request from another web site is refused), every page it serves carries a Content-Security-Policy and may only be shown inside Home Assistant, and stalled or oversized requests are dropped. No visible change in this app.
- **State files are private.** Everything the app saves in `/data` is written atomically and readable by the app only.
- **The Supervisor token is masked** if it ever reaches a log line.

## 0.1.8

- **Shared panel update:** buttons can now upload files (SSL Cert Manager uses this to import certificates); the panel code stays identical across this repository's apps. No visible change in this app.

## 0.1.7

- **Panel:** can open external pages (e.g. a sign-in) in a new browser tab; the shared panel code is kept identical across this repository's apps. No visible change in this app.

## 0.1.6

- **Startup warning removed.** The service is now registered in the `user` bundle under `/etc/s6-overlay/user-bundles.d` instead of the deprecated `/etc/s6-overlay/s6-rc.d/user`, so s6-overlay no longer prints `defining user bundles in /etc/s6-overlay/s6-rc.d is deprecated`. No functional change.
- **Config cleanup.** Removed options that already match Home Assistant defaults, so the app passes the community app config linter. No behaviour change.

## 0.1.5

- Shared panel update: buttons can ask for a line of text, and cards can list plain rows without a button (used by API Key Manager). This app's panel looks the same.

## 0.1.4

- **`!secret` works with live changes.** Options that use `!secret name` are now resolved when you edit them in the Configuration tab or flip a panel switch. Before, the app read the literal `!secret …` text as the value. A panel switch now writes the reference back, never the secret value, and does nothing if Home Assistant can't be reached. A new secret needs an app restart. See *Passwords and secrets* in the docs.
- **Log level** (`log_level`, default `info`): `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. It applies without a restart.
- `scan_minutes` now goes up to 10080 (7 days).

## 0.1.3

- Shared panel update: optional large figures, progress bars, bar charts and a collapsible switch list (used by TNB Usage Checker). This app's panel looks the same.

## 0.1.2

- Lighter scans:
  - Automation and script configs are cached and only re-read after they are edited or reloaded.
  - A scan keeps only the states it needs.
  - The loop sleeps until the next job instead of waking every 10 s.
- The panel stops polling while it is hidden (another tab, or the sidebar panel closed).
- Python bytecode is precompiled in the image, for a faster start with nothing written at runtime.

## 0.1.1

- **Runs as a proper s6-overlay service.** No `legacy-*` or `s6-rc.d is empty` log noise, and the app stops cleanly.
- **Icons** on sections, cards, linked items, switches and buttons in the panel. The store icon is now 128×128 and the logo 250×100.

## 0.1.0

- Initial release.
- Inventory of Home Assistant Companion app devices: last seen, battery, OS, notify service, registration state.
- Linked automations, scripts and scenes, found through the device, its sensors or calls to its notify service. Each has an **Edit** button that opens it in Home Assistant.
- Stale, duplicate and not-loaded flags, plus an **Ignore** button for devices you rarely use.
- Safe removal: the app re-checks the device right before removing it and asks for confirmation. It only removes devices that nothing uses, and never force-removes.
- Attention notifications when devices turn stale or scanning keeps failing, with a switch per phone. Phones are found automatically.
