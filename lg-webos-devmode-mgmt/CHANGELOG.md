# Changelog

## 1.7.7

- Shared panel update: buttons can ask for a line of text, and cards can list plain rows without a button (used by API Key Manager). This app's panel looks the same.

## 1.7.6

- **`!secret` works with live changes.** Options that use `!secret name` are now resolved when you edit them in the Configuration tab or flip a panel switch. Before, the app read the literal `!secret …` text as the value. A panel switch now writes the reference back, never the secret value, and does nothing if Home Assistant can't be reached. A new secret needs an app restart. See *Passwords and secrets* in the docs.
- **Custom renewal `url` is masked** in the Configuration tab and must be `https://`, since it carries the session token. An `http://` URL is now a configuration error.
- **Log level** (`log_level`, default `info`): `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. It applies without a restart.

## 1.7.5

- **LG maintenance windows** (`maintenance_windows`, default `daily 00:00-06:00`, Home Assistant's timezone). No renewal is attempted inside a window: a renewal due then waits until it ends, and **Renew now** says when that is. The Schedule card shows the windows.
- **Back-off when LG's developer site is down for maintenance.** An HTTP 503 or a maintenance page shows **LG maintenance** on the TV, and it isn't counted as a failure, so no alert is sent. Retries come after 30 min, doubling up to 6 h, then the normal interval resumes after the next success.

## 1.7.4

- **Fixed the Supervisor warning** `Option 'notification_overrides' does not exist in the schema`, logged every 30 seconds. The one-time move of the old per-phone switches into the panel looked for the option in `/data/options.json`, which the Supervisor already filters to the schema, so it never ran. It now reads the stored options, keeps any phone switch you already set in the panel, and removes the option.
- **Shared panel update:** optional large figures, progress bars, bar charts and a collapsible switch list (used by TNB Usage Checker). This app's panel looks the same.

## 1.7.3

- The panel stops polling while it is hidden (another tab, or the sidebar panel closed).
- Python bytecode is precompiled in the image, for a faster start with nothing written at runtime.

## 1.7.2

- **No more crash loop on a bad configuration** (for example no TVs yet). The app stays up, and the panel shows what to fix with an example. It starts by itself once the options are valid.
- **Runs as a proper s6-overlay service.** The `legacy-cont-init` / `legacy-services` / `s6-rc.d is empty` log noise is gone, and a crash stops the app cleanly so the Supervisor watchdog can restart it.
- **Icons** on sections, cards, switches and buttons in the panel. The store icon is now 128×128 and the logo 250×100, with the new name.

## 1.7.1

- Renamed from **LG webOS Dev Session Auto Renew** to **LG webOS DevMode Mgmt** (sidebar: **LG DevMode**). The repository folder is now `lg-webos-devmode-mgmt/`. The slug, image, options and data are unchanged, so there is nothing to reconfigure.
- The panel can link straight to Home Assistant settings pages and ask for confirmation before an action (shared panel update).

## 1.7.0

- Phones are no longer typed into the Configuration tab. The app lists the Companion apps linked to this Home Assistant automatically (at start and every 5 minutes): new phones are notified by default and unlinked phones disappear.
- Per-phone switches live only in the panel's **Phones** card and are kept in `/data/phones.json`. Existing `notification_overrides` choices are migrated once and the option is removed.
- Log lines use local time from the first line (the container `TZ` is loaded before logging starts).

## 1.6.0

- New sidebar panel (ingress) styled like Home Assistant. It uses the live HA theme, including dark mode, and works on phones.
  - **TV cards:** status, last attempt, last success, next renewal, an **Auto renew** switch and a **Renew now** button.
  - **Schedule card:** a **Failure notifications** switch and a **Renew all now** button.
  - **Phones card:** one notification switch per phone.
- Switches apply immediately without a restart. Each one is saved to the app options, so it also shows in the Configuration tab. Configuration-tab edits (TVs, tokens, interval, retries) are now hot-reloaded within about 30 seconds.
- New optional per-session `enabled` option ("Auto renew"). A paused TV keeps its token and is skipped by the schedule.
- Discovered phones are written back to the options only when a new phone appears, not on every start.
- `BUILD_VERSION` is now passed into the image, so the panel shows the real version.

## 1.5.1

- Fix persisting the per-phone toggles: `POST /addons/self/options` now sends the payload as `{"options": {...}}`, so the merged `notification_overrides` actually reach the Configuration page instead of failing with HTTP 400 (they already applied in-memory for the current run).

## 1.5.0

- Add per-phone notification toggles: every discovered mobile app appears as a `notification_overrides` row with its own `notify` switch in the Configuration page, all on by default. Newly discovered phones are added with `notify: true` and existing rows keep their saved value (via `POST /addons/self/options`), so switching a phone off persists across restarts. Effective targets = discovered phones minus any rows with `notify: false`.
- Privacy: renewal URLs and the session token (masked or not) are no longer written to the logs. Startup and renewal lines now log only the session name; the `mask_token` helper was removed.

## 1.4.5

- Strip the trailing newline from s6 container-environment values so the merged `SUPERVISOR_TOKEN` produces a valid Authorization header.

## 1.4.4

- Fix supervisor token availability: merge the s6 container environment (`/run/s6/container_environment`) into the process environment before lookup, replicating `with-contenv` without needing an s6 shell wrapper.

## 1.4.3

- Diagnose missing container environment: when `SUPERVISOR_TOKEN` is unavailable, the add-on logs the full set of environment variable names and the contents of `/run` and `/run/supervisor` so the supervisor token mechanism can be identified.

## 1.4.2

- Grant `hassio_api` access and add supervisor-token diagnostics: when `SUPERVISOR_TOKEN` is missing from the container environment, the startup log now names the supervisor-related env vars that are present, making missing-API failures visible instead of silent.

## 1.4.1

- Make mobile-app target discovery resilient: targets are now drawn straight from the registered Home Assistant `notify` services (`notify.mobile_app_*`), so it keeps working even when the add-on token cannot list `device_tracker` entities.

## 1.4.0

- Replace the manual `notification_targets` list with automatic discovery of mobile apps: the add-on pulls the phone device names from the existing Home Assistant `device_tracker` entities and notifies every `notify.mobile_app_*` target that is registered (falling back to a persistent notification when none exist). Removes the `notification_targets` option from the Configuration page.

## 1.3.0

- Modernize the add-on Configuration page: add `translations/en.yaml` with a friendly label and description for every option (including the per-session fields), and update the schema so the dev-mode session `token` renders as a masked password input and the session `url` is validated as a URL.

## 1.2.0

- Failure notifications can now be pushed to Home Assistant mobile apps via `notification_targets` (list of `notify` entities such as `mobile_app_pixel_8`; a `notify.` prefix is stripped). When targets are configured they replace the persistent notification; with no targets the persistent notification behavior is unchanged.

## 1.1.0

- Move to the standalone public repository `nerowentah/haos-addons`; the add-on now lives at the repository root (`lg-webos-auto-renew/`) and is distributed as a prebuilt multi-arch image (`ghcr.io/nerowentah/lg-webos-auto-renew`) instead of being built on-device.
- Update for Home Assistant / Supervisor 2026.09:
  - Drop deprecated `build.yaml`; build directly from the multi-arch `ghcr.io/home-assistant/base:3.24` (Alpine) image.
  - Drop `armv7` support (deprecated in Supervisor 2026.09); supported architectures are now `aarch64` and `amd64`.
  - Repair the options schema so each session may provide a `token` **or** a `url` (both are optional).
  - Add `icon.png` / `logo.png` (2026.09 renders only PNG store assets).
- Repository moved from `Extensions/homeassistant-os/lg-webos-auto-renew` to `homeassistant-os/lg-webos-auto-renew`.

## 1.0.0

- Initial release.
- Multi-session support: each session accepts a `token` (dev-mode enable key) or a full LG API `url`.
- Configurable interval (`interval_hours` 1-168), per-session retries with exponential backoff.
- Optional Home Assistant `persistent_notification` when a renewal fails after all retries.
- Stdlib-only Python runner (no pip dependencies).
- Tokens are masked in all log output.
