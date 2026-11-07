# Changelog

## 0.1.0

- Initial release.
- Inventory of Home Assistant Companion app devices: last seen, battery, OS, notify service, registration state.
- Linked automations, scripts and scenes, found through the device, its sensors or calls to its notify service. Each has an **Edit** button that opens it in Home Assistant.
- Stale, duplicate and not-loaded flags, plus an **Ignore** button for devices you rarely use.
- Safe removal: the app re-checks the device right before removing it and asks for confirmation. It only removes devices that nothing uses, and never force-removes.
- Attention notifications when devices turn stale or scanning keeps failing, with a switch per phone. Phones are found automatically.
