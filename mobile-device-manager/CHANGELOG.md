# Changelog

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
