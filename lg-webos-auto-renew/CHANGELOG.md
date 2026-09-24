# Changelog

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
