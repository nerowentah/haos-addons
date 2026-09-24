# Changelog

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
