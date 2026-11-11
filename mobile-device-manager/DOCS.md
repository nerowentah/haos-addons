# Mobile Device Manager

Every phone or tablet you sign in to with the Home Assistant **Companion app** becomes a device, with sensors and a `notify.mobile_app_*` service. Old phones, reinstalls and renamed devices leave registrations behind. Automations often still notify a phone you no longer own. This app shows all of that in one place and lets you clean it up safely.

## Installation

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **Mobile Device Manager** and **Start** it. No configuration is needed.
3. Open **Mobile Devices** from the sidebar. Only administrators see it.

## Configuration

```yaml
scan_minutes: 30
stale_days: 30
notify_on_attention: true
ignored_devices: []
```

| Option | Default | Description |
| --- | --- | --- |
| `scan_minutes` | `30` | How often to rescan (5-10080 minutes, up to 7 days). The panel also has **Rescan now**. |
| `stale_days` | `30` | A device that has reported nothing for this many days is flagged **Stale** (1-365). |
| `notify_on_attention` | `true` | Notify when devices turn stale or scanning fails 3 times in a row (and again when it recovers). |
| `log_level` | `info` | How much is logged: `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. Use `debug` while troubleshooting. Applies without a restart. |
| `ignored_devices` | `[]` | Device ids never flagged stale, for example a tablet you rarely use. The **Ignore** button in the panel fills this in. |

Changes made in the Configuration tab are picked up within about 30 seconds. You don't need to restart the app.

## Panel

### Overview

The overview shows:
- how many devices there are
- how many are stale, duplicated, not loaded or in use
- the last and next scan

It also has a **Rescan now** button.

### Device cards

**Header:** the name, maker, model and OS.

**Badge:**

| Badge | Meaning |
| --- | --- |
| **Active** | Seen recently, nothing wrong. |
| **Stale** | Not seen for more than `stale_days`. |
| **Duplicate** | Another device has the same name and model, and this one was seen less recently. It is usually a leftover from reinstalling the app. |
| **Not loaded** | The Companion app registration failed to load in Home Assistant. |
| **Ignored** | Excluded from stale warnings. |

**Stats:**
- **Last seen:** the newest report from any of the device's sensors.
- **Battery**
- **Notify service:** for example `mobile_app_pixel_9`.
- **Used by:** how many linked items there are.

**Linked items** list everything that still depends on the phone:
- automations, scripts and scenes that use the **device or its sensors** (as triggers, conditions or targets)
- automations and scripts that **send notifications** to its `notify.mobile_app_*` service

**Edit** opens the item's editor in Home Assistant, inside the same window. Items written in YAML can't be edited in the UI. For those, the button opens the automation or script list, and you change them in your configuration files.

**Buttons:**
- **Open device:** the Home Assistant device page.
- **Ignore / Unignore:** exclude the device from stale warnings, or include it again.
- **Remove:** see below.

### Removing a device

**Remove** is only available when the device has **no linked items**. If anything still uses it, the button is disabled and the card says what to edit first. There is no way to force it.

When you press **Remove**:
1. The app asks you to confirm.
2. It re-checks the device against Home Assistant right before removing it. If something started using the device in the meantime, the removal is refused, with the list of items.
3. It deletes the device's Companion app registration. This removes the device and all of its entities from Home Assistant.

The phone itself is not affected. To use it again, sign in again from the Companion app.

### Settings and Phones cards

- **Attention notifications** switches notifications on or off.
- **Phones** has one switch per Companion app linked to this Home Assistant. Phones are found automatically every 5 minutes, and new phones are switched on.

## How the scan works

1. It reads the `mobile_app` config entries and the device and entity registries.
2. It reads entity states, for last seen and battery.
3. It reads the `notify` services, to match each device's notify service.
4. It asks Home Assistant's `search/related` API what uses each device.
5. It reads the stored configs of UI-managed automations and scripts and looks for the device's notify service.

**Limitation:** notify calls inside YAML-only automations and scripts (in `automations.yaml` packages or `configuration.yaml`) can't be read through the API. Check those by hand before removing a device.

## Notifications

- Devices that newly turn stale are reported once, in a single notification. A device is reported again only after it has been active in between.
- Scan failures are reported after 3 failed scans in a row, and again when scanning recovers.
- Without any linked phones, the app creates a persistent notification instead.

## Passwords and secrets

- **Where they live:** Home Assistant's Supervisor stores the app options, including its options (it has no passwords). The app keeps no copy: its own file `/data/state.json` never holds a password, token or account number. Home Assistant encrypts its backups.
- **Masked:** password fields show as dots in the Configuration tab.
- **Never logged:** passwords, tokens and cookie values never appear in the log or the panel, and numbers are masked.
- **`!secret`:** to keep a value out of the app options, put it in `/config/secrets.yaml` and reference it, for example `ignored_devices: ["!secret old_tablet"]`.
  - The panel switches and live edits keep the reference as it is. The app never writes the value back.
  - A new or changed secret takes effect after you **restart** the app, because the Supervisor reads `secrets.yaml` only at start. Until then the panel keeps the old value and the log says a restart is needed.
- Anyone who can open the app's Configuration tab, or log in to the host, can still see the values. Give the Home Assistant admin role only to people you trust.

## Troubleshooting

- **What do the `s6-rc` lines in the log mean?** The app runs as an s6-overlay service, the process supervisor built into Home Assistant app images. Only s6 warnings and errors are logged. Older versions printed `legacy-cont-init`, `legacy-services` and `s6-rc.d is empty` on every start. Those were harmless, and came from running without a proper service definition. The service is registered in the `user` bundle under `/etc/s6-overlay/user-bundles.d`, so the old `defining user bundles ... is deprecated` line no longer appears.
- **No devices listed:** check that the Companion app integration (**Settings → Devices & services → Mobile App**) has entries.
- **"Scan failing":** the panel shows the error. The app needs the Home Assistant API, which is enabled by default.
- **Remove refused:** open each linked item with **Edit** and remove the device, its sensors or its notify service from it. Then press **Rescan now**.
