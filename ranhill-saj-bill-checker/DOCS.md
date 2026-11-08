# Ranhill SAJ Bill Checker

Ranhill SAJ Bill Checker shows the **amount due** on one or more Ranhill SAJ (Johor) water accounts in Home Assistant.

It uses the public **Quick Payment** page at ranhillsaj.com.my, which needs no login. You enter an account number there and it shows the holder's name and the amount due before you pay. The app does the same lookup on a schedule. It never goes past that step, so **it never starts a payment**.

> **Disclaimer:** Ranhill SAJ has no public API. The Quick Payment page is used unofficially and may change without notice. Your bill is authoritative.

## Requirements

- Home Assistant OS or Supervised.
- The 16-character account number from your Ranhill SAJ bill.
- Optional: the **Mosquitto broker** app and the MQTT integration. See [MQTT or not](#mqtt-or-not).

## Installation

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **Ranhill SAJ Bill Checker**.
3. Open the **Configuration** tab, add your accounts (below), then **Start** the app.
4. Open **SAJ Water** from the sidebar to see the panel.

## Configuration

```yaml
poll_interval_hours: 6
notify_on_failure: true
mqtt: true
accounts:
  - name: "Home"
    account_number: "12345678-A1234567"
  - name: "Shop"
    account_number: "12345678 B 7654321"   # spaces and hyphens are fine
```

### The account number

It is printed on your bill as **No. Akaun**. It has 16 characters: **8 digits, a letter, then 7 digits**, for example `12345678-A1234567`. Upper or lower case, spaces and hyphens are all accepted.

### Global options

| Option | Default | Description |
| --- | --- | --- |
| `poll_interval_hours` | `6` | How often to look up each account (1–24 hours). Bills are monthly, so a few times a day is plenty. |
| `notify_on_failure` | `true` | Notify your phones when checks fail and when they recover. |
| `mqtt` | `true` | Use MQTT discovery when a broker is available. See [MQTT or not](#mqtt-or-not). |

### Account options

| Option | Required | Description |
| --- | --- | --- |
| `name` | yes | Friendly name. It also sets the entity ids, so `Home` gives `sensor.saj_home_amount_due`. |
| `account_number` | yes | The 16-character number from the bill. |
| `enabled` | no | `false` pauses checks for this account. Easier to switch from the panel. |

### If the configuration has a problem

The app does not crash. The **SAJ Water** panel shows what is wrong, with an example. Fix the option and save. The app starts by itself within 15 seconds, with no restart needed.

## Panel

Open **SAJ Water** from the sidebar.

- **One card per account:**
  - amount due
  - masked account number and holder initials
  - when it was last checked and when the amount last changed
  - a **Check now** button and an on/off switch
  - when a check fails, the reason, shown on the card
- **Settings:** the check interval, how entities reach Home Assistant, and switches for MQTT and failure notifications.
- **Phones:** every Companion app linked to your Home Assistant, each with its own notification switch. New phones are switched on.

Switches apply immediately and are saved to the app options. Edits in the Configuration tab are picked up within about 30 seconds.

## MQTT or not

| | With MQTT (Mosquitto broker app installed) | Without MQTT |
| --- | --- | --- |
| Entities | One **device** per account, entities can be renamed and assigned to areas | Plain entities with the same `sensor.saj_*` ids, no device |
| After a Home Assistant restart | Come back immediately (retained discovery) | Come back within 5 minutes |

- The app checks for a broker every minute.
- **Broker installed later:** the app switches to MQTT within a minute, with no restart. The plain entities are removed first, so the entity ids stay the same.
- **MQTT switched off:** turn off **Use MQTT when available** in the panel, or set `mqtt: false`. The MQTT devices are removed and the plain entities take over.
- **Which mode is active:** the panel's **Entities via** shows it.

## Entities

| Entity | Description |
| --- | --- |
| `sensor.saj_<id>_amount_due` | Amount due in RM. A credit shows as a negative number. The attributes hold the masked account number and holder initials. |
| `sensor.saj_<id>_amount_changed` | When the amount due last changed, which is usually when a new bill was issued or a payment was posted |
| `sensor.saj_<id>_status` | Diagnostic: `ok`, `pending`, `not_found`, `layout_changed` or `error` |
| `sensor.saj_<id>_last_checked` | Diagnostic: when the last check ran |

A paused account shows its entities as unavailable.

### Example: tell me when a new bill arrives

```yaml
automation:
  - alias: "New water bill"
    triggers:
      - trigger: state
        entity_id: sensor.saj_home_amount_due
    conditions:
      - condition: template
        value_template: >-
          {{ trigger.from_state.state not in ['unknown', 'unavailable']
             and trigger.to_state.state | float(0) > trigger.from_state.state | float(0) }}
    actions:
      - action: notify.notify
        data:
          title: "Ranhill SAJ"
          message: "Water bill is now RM {{ trigger.to_state.state }}"
```

## Notifications

When an account cannot be checked, and again when it recovers, the app notifies every phone that is switched on in the **Phones** card. It sends one notification per change, not one per retry. With no linked phones, it creates a persistent notification instead.

- **Unknown account number:** notifies at once.
- **Site errors:** notify after 3 failed checks in a row. The app retries every 30 minutes until it works.

## Troubleshooting

- **`not_found` (Account not found):** Ranhill SAJ does not recognise the number.
  - Compare it with the bill. It must be 8 digits, a letter and 7 digits.
  - Try the same number at ranhillsaj.com.my → **Pay Bills** in a browser.
  - The app checks again after one full interval, or when you press **Check now**.
- **`layout_changed` (Site changed):** the page loaded, but it no longer looks the way the app expects.
  - The app saves that page to `/data/last_unrecognised_page.html` inside the app (it may contain your name and address).
  - Please open an issue so the parser can be updated. Don't attach the page unless you have removed your details from it.
- **`error` (Site error):** the site was slow, down or blocked the request. It usually clears on its own.
- **No entities:**
  - Check **Entities via** in the panel.
  - **With MQTT:** make sure the Mosquitto broker and the MQTT integration are running.
  - **Without MQTT:** the entities come back within 5 minutes of a Home Assistant restart.
- **What do the `s6-rc` lines in the log mean?** The app runs as an s6-overlay service. Only s6 warnings and errors are logged.
- **Icons missing in the app store:** open **⋮ → Check for updates** in the app store. Home Assistant caches app icons per repository refresh.
- **Privacy:**
  - The panel, logs and entities show the account number masked (first and last 4 characters) and only the initials of the holder's name.
  - The address shown on the Quick Payment page is never read or stored.
  - The full number is kept only in the app options.

Not affiliated with Ranhill SAJ Sdn. Bhd.
