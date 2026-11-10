# Malaysia Utility Bills

Malaysia Utility Bills tracks your Malaysian utility accounts in one app, with one panel and one MQTT connection:

- **TNB electricity**:
  - **Imports myTNB smart meter data.** It logs in to the myTNB portal and pulls hourly usage (kWh) and cost (RM). These become long-term statistics you can pick in the **Energy dashboard**. Each sync also reads the amount due.
  - **Estimates the running bill live** from a local energy sensor such as a Shelly EM or CT clamp. It uses the 2025 TNB domestic tariff: General or Time-of-Use, capacity, network, retail, AFA, EEI, KWTBB, SST and NEM export credit.
- **Ranhill SAJ water (Johor)**: checks the **amount due** on each account weekly. It uses the public **Quick Payment** page, which needs no login, and it never starts a payment.

TM Unifi will be added to this app as another provider.

This app replaces **TNB Usage Checker** and **Ranhill SAJ Bill Checker**, and keeps their entity ids, history and Energy dashboard statistics. See [Moving from the old apps](#moving-from-the-old-apps).

> **Disclaimer:** neither myTNB nor Ranhill SAJ has a public API. Their websites are used unofficially and may change without notice. Bill estimates are indicative only. Your bills are authoritative.

## Requirements

- Home Assistant OS or Supervised.
- **TNB:**
  - For myTNB data: a smart meter (AMI) and a myTNB login.
  - For the live estimate: a cumulative grid-import energy sensor (kWh, Wh or MWh).
- **Ranhill SAJ:** the 16-character account number from your bill.
- Optional: the **Mosquitto broker** app and the MQTT integration. See [MQTT or not](#mqtt-or-not).

## Installation

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **Malaysia Utility Bills**.
3. Either add your accounts in the **Configuration** tab (below), or start the app and import them from the old apps (see [Moving from the old apps](#moving-from-the-old-apps)).
4. **Start** the app and open **Utility Bills** from the sidebar.

## Moving from the old apps

The old apps keep working, so you can move one provider at a time.

1. Install and start this app. With no accounts yet, its panel shows **Get started** and, under **Settings**, a **Moving from the old apps** card.
2. Press **Import from TNB Usage Checker** and/or **Import from Ranhill SAJ Bill Checker**.
   - The app copies that app's accounts and options into this app's `tnb_` or `saj_` options. `!secret` references are copied as references.
   - If an imported option uses a `!secret` this app has not loaded yet, restart this app once.
   - The import only fills an empty `tnb_accounts` or `saj_accounts`; it never overwrites accounts you already added.
3. **Stop the old app.** While an old app is running, this app leaves that provider alone and its section says **Waiting for … to stop**. Both apps would otherwise write the same entities.
4. Within 5 minutes this app takes over. Every entity keeps its id (`sensor.tnb_*`, `binary_sensor.saj_*` …), its history, area and name. Energy dashboard statistics (`power_tnb:usage_<id>`, `power_tnb:cost_<id>`) carry on without a gap.
5. When you're happy, uninstall the old app.

What starts over:

- The panel's daily usage chart and the reminder clock for unpaid bills. Each app keeps these in its own storage, which another app can't read. They come back after the first sync.
- The live TNB bill estimate restarts from the energy sensor's current reading. The rest of the current cycle is counted from now on, and myTNB's own cycle figures are unaffected.
- Phone switches: every phone starts switched on.

If the import button reports that it can't read the old app's options, copy them by hand: each old option `x` becomes `tnb_x` (TNB) or `saj_accounts` (Ranhill SAJ). `notify_on_failure`, `notify_on_unpaid`, `mqtt` and `log_level` keep their names.

## Configuration

```yaml
notify_on_failure: true
notify_on_unpaid: true
mqtt: true
log_level: info

# TNB electricity
tnb_mytnb_username: "you@example.com"   # one myTNB login for every TNB account
tnb_mytnb_password: "!secret mytnb_password"
tnb_accounts:
  - name: "Home"
    account_number: "123456789012"   # the 12-digit number on the bill
    tariff: "tou"
    billing_start_day: 12
    import_entity: "sensor.shelly_em_channel_1_energy"
    export_entity: "sensor.solar_export_energy"
  - name: "Parents"                  # belongs to a different myTNB login
    account_number: "123456789014"
    tariff: "general"
    billing_start_day: 5
    mytnb_username: "parents@example.com"
    mytnb_password: "!secret parents_mytnb_password"
tnb_portal_interval_hours: 6
tnb_meter_poll_seconds: 60
tnb_afa_rate: -0.0047               # RM/kWh; update monthly from TNB/ST announcements
tnb_calendarific_api_key: ""        # optional
tnb_extra_holidays:
  - "2026-03-21"                    # e.g. Hari Raya Aidilfitri
tnb_daily_report: true
tnb_maintenance_windows:
  - "daily 00:00-06:00"
tnb_debug_login: false

# Ranhill SAJ water
saj_accounts:
  - name: "Home"
    account_number: "12345678-A1234567"
  - name: "Shop"
    account_number: "12345678 B 7654321"   # spaces and hyphens are fine
```

Leave a provider's account list empty if you don't use it. Changes made in the Configuration tab are picked up within about 30 seconds. You don't need to restart the app.

### Shared options

| Option | Default | Description |
| --- | --- | --- |
| `notify_on_failure` | `true` | Notify when a myTNB sync or a Ranhill SAJ check keeps failing, and when it recovers. Also a panel switch. |
| `notify_on_unpaid` | `true` | Notify when an account shows a new amount due, remind once a week while it stays unpaid, and say when it is paid. Also a panel switch. |
| `mqtt` | `true` | Use MQTT discovery when a broker is found. See [MQTT or not](#mqtt-or-not). |
| `log_level` | `info` | How much is logged: `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. Use `debug` while troubleshooting. Applies without a restart. |

### TNB options

| Option | Default | Description |
| --- | --- | --- |
| `tnb_mytnb_username`, `tnb_mytnb_password` | empty | The shared myTNB login, used by every TNB account that has an `account_number` and no login of its own. |
| `tnb_accounts` | `[]` | One entry per electricity account (below). |
| `tnb_portal_interval_hours` | `6` | Hours between myTNB syncs (1-168, up to 7 days). TNB data is about 2 days behind. The amount due is read on every sync. |
| `tnb_meter_poll_seconds` | `60` | How often each account's energy sensor is read (10-600). |
| `tnb_afa_rate` | `0.0` | Automatic Fuel Adjustment in RM/kWh. A negative value is a rebate. It only applies above 600 kWh. |
| `tnb_calendarific_api_key` | `""` | Optional key for lunar public holidays (ToU off-peak). |
| `tnb_extra_holidays` | `[]` | Extra `YYYY-MM-DD` dates treated as off-peak all day. |
| `tnb_daily_report` | `true` | Once per account and day, when a sync brings in a new complete day, notify the phones with that day's kWh and RM, the change from the day before, and the cycle so far. myTNB data is about 2 days behind, so the report is about the day before yesterday. Also a panel switch. |
| `tnb_maintenance_windows` | `daily 00:00-06:00` | When TNB does system maintenance (Malaysia time), one per line: `daily 00:00-06:00`, `sun 01:00-05:00`, `mon-fri 23:00-02:00` (crosses midnight), `weekends 02:00-04:00` or `mon,wed 00:00-24:00`. The app never calls myTNB inside a window. A sync due then waits until the window ends, plus a few random minutes. An empty list allows syncing at any time. |
| `tnb_debug_login` | `false` | Logs every myTNB login step: redirects, HTTP status, page titles, and cookie and form field names. Passwords, cookie and form values, and account numbers are never logged. Also a panel switch. |

**TNB account options:**

| Option | Required | Description |
| --- | --- | --- |
| `name` | yes | Friendly name. It also sets the ids: `Home` gives the device `TNB Home` and entities `sensor.tnb_home_*`. Names must be unique. |
| `tariff` | yes | `general` or `tou`. |
| `billing_start_day` | yes | Day of the month your billing cycle starts (1-31). Short months use their last day. |
| `account_number` | for myTNB | The 12-digit TNB account number from your bill (also in the myTNB app). Enables myTNB sync with the shared login. Spaces and dashes are ignored. |
| `mytnb_username`, `mytnb_password` | optional | Only for an account under a different myTNB login. Set both together. |
| `smartmeter_url` | optional | The address of **View Energy Usage** for this account on the myTNB website. Only needed if sync reports `meter_error`. Keep `account_number` as well: it picks this account's amount due on the myTNB dashboard. |
| `import_entity` | optional | Cumulative grid-import energy sensor (kWh, Wh or MWh). Enables the live estimate. |
| `export_entity` | optional | Cumulative grid-export energy sensor, for NEM. Needs `import_entity`. |
| `enabled`, `portal_enabled`, `meter_enabled`, `statistics_enabled` | optional | On/off switches, all on by default. These are the panel switches. |

Every TNB account needs an `account_number` (with a myTNB login), an `import_entity`, or both. Each account gets its own portal session, and accounts sync one after another, never in parallel.

### Ranhill SAJ options

| Option | Required | Description |
| --- | --- | --- |
| `saj_accounts` → `name` | yes | Friendly name. It also sets the entity ids, so `Home` gives `sensor.saj_home_amount_due`. |
| `saj_accounts` → `account_number` | yes | Printed on your bill as **No. Akaun**: 16 characters, **8 digits, a letter, then 7 digits**, for example `12345678-A1234567`. Upper or lower case, spaces and hyphens are all accepted. |
| `saj_accounts` → `enabled` | no | `false` pauses checks for this account. Easier to switch from the panel. |

Each SAJ account is checked **once a week**, because bills are monthly. Press **Check now** in the panel any time, for example right after paying. After a restart the weekly schedule continues from the last check. An unknown account number or a changed page is retried daily.

### If the configuration has a problem

The app keeps running, and the panel shows **Configuration needs attention**. That page names the provider and the exact problem (for example *"TNB (tnb_ options): Account 'Home': add account_number, the 12-digit number printed on your TNB bill"*) and shows an example. Fix the option in the Configuration tab and save. The app starts within 15 seconds, with no restart.

## Panel

Open **Utility Bills** from the sidebar, or click **Open web UI** on the app page. Only administrators see it. It follows your Home Assistant theme, including dark mode, and works on phones.

- **Electricity · TNB** has one card per account:
  - the bill estimate and predicted bill, usage for the cycle and today, the peak/off-peak split and a daily usage chart
  - myTNB cycle usage and cost, amount due, data freshness and sync status
  - a **Sync myTNB now** button, and switches for the **Account**, **myTNB sync**, **Energy dashboard import** and **Live bill estimate**
- **Water · Ranhill SAJ** has one card per account:
  - amount due, *Due* or *Paid*, the masked account number and holder initials, last checked and last change
  - a **Check now** button and an on/off switch
- **Settings:**
  - **Entities via**, which shows whether MQTT or the states API is in use, and the shared switches for MQTT, failure notifications and unpaid-bill notifications
  - a **TNB** card with AFA, today's holiday status, the sync intervals and maintenance windows, and the daily report and verbose login switches
  - a **Ranhill SAJ** card with the check schedule
  - **Phones:** one switch per Companion app linked to this Home Assistant, found automatically. New phones are on.
  - **Moving from the old apps**, while either old app is installed

A switched-off account or source keeps its entities. They show as *unavailable* instead of being deleted. Account and settings switches are saved into the app options, so they also show in the Configuration tab. Phone switches are kept by the app itself. Every switch applies immediately.

## MQTT or not

| | With MQTT (Mosquitto broker app installed) | Without MQTT |
| --- | --- | --- |
| Entities | One **device** per account, entities can be renamed and assigned to areas | Plain entities with the same ids, no device |
| After a Home Assistant restart | Come back immediately (retained discovery) | Come back within 5 minutes |
| Energy dashboard statistics | Yes | Yes (statistics don't depend on MQTT) |

- The app checks for a broker every minute.
- **Broker installed later:** the app switches to MQTT within a minute, with no restart. The states-API entities are removed first, so the entity ids stay the same.
- **MQTT switched off:** turn off **Use MQTT when available** in the panel, or set `mqtt: false`. The MQTT devices are removed and the plain entities take over.
- The app uses one MQTT connection. Its status topic is `malaysia_utility_bills/status`. Each provider keeps its old topics (`power_tnb/…`, `ranhill_saj/…`).

## Entities

### TNB, from myTNB (if credentials are set)

| Entity | Description |
| --- | --- |
| `sensor.tnb_<id>_portal_usage_cycle` / `_portal_cost_cycle` | kWh and RM so far in the current billing cycle, according to TNB |
| `sensor.tnb_<id>_portal_usage_latest_day` / `_portal_cost_latest_day` | The most recent day TNB has published |
| `sensor.tnb_<id>_amount_due` | Amount due in RM. A credit shows as a negative number. |
| `sensor.tnb_<id>_due_date` | When the amount is due, if myTNB shows it |
| `binary_sensor.tnb_<id>_payment_due` | On while an amount is due, off once it is paid |
| `sensor.tnb_<id>_portal_data_as_of`, `_portal_last_sync`, `_portal_status` | Diagnostics |

### TNB, from the energy sensor (if `import_entity` is set)

| Entity | Description |
| --- | --- |
| `sensor.tnb_<id>_bill_estimate` | Estimated bill so far. The attributes hold the full breakdown and past cycles. |
| `sensor.tnb_<id>_predicted_bill` / `_predicted_kwh` | Projected bill and usage at the end of the cycle |
| `sensor.tnb_<id>_cycle_import` / `_cycle_peak` / `_cycle_offpeak` / `_cycle_export` | Billing-cycle kWh |
| `sensor.tnb_<id>_today_import` / `_today_export` | kWh today |
| `sensor.tnb_<id>_current_rate` | Approximate RM cost of the next kWh |
| `sensor.tnb_<id>_current_period` | `peak`, `off-peak` or `flat` |
| `sensor.tnb_<id>_tier` | `1` (≤1500 kWh) or `2` |
| `sensor.tnb_<id>_days_remaining`, `_nem_carry_kwh` | Diagnostics |

There is also one shared TNB entity: `binary_sensor.tnb_holiday_today`.

**Billing cycles and safeguards:** each cycle follows the account's `billing_start_day`; finished cycles are archived (the last 12 are kept) and NEM excess is carried forward. A meter that resets is re-baselined automatically, and unrealistic jumps (more than 10 kWh per 5 minutes) are ignored.

### Ranhill SAJ

| Entity | Description |
| --- | --- |
| `sensor.saj_<id>_amount_due` | Amount due in RM. A credit shows as a negative number. The attributes hold the masked account number and holder initials. |
| `binary_sensor.saj_<id>_payment_due` | On while an amount is due, off once it is paid (or in credit) |
| `sensor.saj_<id>_amount_changed` | When the amount due last changed, usually when a new bill was issued or a payment was posted |
| `sensor.saj_<id>_status` | Diagnostic: `ok`, `pending`, `not_found`, `layout_changed` or `error` |
| `sensor.saj_<id>_last_checked` | Diagnostic: when the last check ran |

## Energy dashboard

1. Open **Settings → Dashboards → Energy → Electricity grid → Add consumption**.
2. Select the statistic **TNB &lt;name&gt; usage** (`power_tnb:usage_<id>`).
3. Under cost, choose **Use an entity tracking the total costs** and select **TNB &lt;name&gt; cost** (`power_tnb:cost_<id>`).

The statistics appear after the first successful sync. Every sync re-imports the last 30 days, so hours TNB publishes late are filled in rather than skipped.

## TNB tariff used

TNB domestic tariff for Peninsular Malaysia, valid 1 July 2025 to 31 December 2027:

| Item | Rate |
| --- | --- |
| Energy, General | 27.03 sen/kWh. Above 1500 kWh the whole bill is charged at 37.03 sen. |
| Energy, ToU | Peak 28.52 / off-peak 24.43 sen. Above 1500 kWh: 38.52 / 34.43 sen. |
| Capacity / Network | 4.55 / 12.85 sen/kWh |
| Retail | RM10 per month, waived at 600 kWh or less |
| AFA | `tnb_afa_rate` × kWh, only above 600 kWh |
| EEI (Energy Efficiency Incentive) | Rebate on the whole bill, from 25 sen/kWh (≤200 kWh) sliding down to 0.5 sen/kWh (≤1000 kWh) |
| KWTBB | 1.6% when above 300 kWh |
| SST | 8% on the share of usage above 600 kWh, plus the retail charge |
| NEM | Exports are credited at the off-peak energy rate plus capacity plus network, up to the import charges. Any excess is carried forward. |

**ToU:** weekdays 14:00-22:00 count as peak, and readings that cross a boundary are split by time. Weekends and public holidays count as off-peak. Fixed-date holidays are built in. Lunar holidays come from `tnb_calendarific_api_key` or the `tnb_extra_holidays` list.

## Notifications

The app notifies every phone that is switched on in the **Phones** card. With no linked phones, it creates a persistent notification instead.

- **Failures** (`notify_on_failure`): one notification when a myTNB sync or a Ranhill SAJ check fails, and one when it recovers, not one per retry. An unknown SAJ account number or a wrong myTNB password notifies at once; site errors after 3 failures in a row.
- **Unpaid bills** (`notify_on_unpaid`): when an amount is due, weekly while it stays unpaid, and once when it is paid.
- **TNB daily report** (`tnb_daily_report`): once a day per account, when myTNB publishes a new complete day.

## Passwords and secrets

- **Where they live:** Home Assistant's Supervisor stores the app options, including `tnb_mytnb_password`, `smartmeter_url`, `tnb_calendarific_api_key` and your account numbers. The app keeps no copy: its own file `/data/state.json` never holds a password, token or account number. Home Assistant encrypts its backups.
- **Masked:** password fields show as dots in the Configuration tab.
- **Never logged:** passwords, tokens and cookie values never appear in the log or the panel, and account numbers are masked.
- **`!secret`:** to keep a value out of the app options, put it in `/config/secrets.yaml` and reference it, for example `tnb_mytnb_password: "!secret mytnb_password"`.
  - The panel switches, live edits and the import keep the reference as it is. The app never writes the value back.
  - A new or changed secret takes effect after you **restart** the app, because the Supervisor reads `secrets.yaml` only at start. Until then the app keeps the old value and the log says a restart is needed.
- Anyone who can open the app's Configuration tab, or log in to the host, can still see the values. Give the Home Assistant admin role only to people you trust.

## Troubleshooting

- **A section says "Waiting for … to stop":** the old app for that provider is still running. Stop it in **Settings → Apps**. This app checks every 5 minutes and then takes over.
- **TNB `maintenance` status:** myTNB answered with a maintenance page or HTTP 503 outside your windows. The app keeps the last data, sends no failure alert, and retries after 30 min, then 1 h, 2 h, 4 h, up to 6 h. If this keeps happening at the same time of day, add that time to `tnb_maintenance_windows`.
- **TNB `auth_error`:** myTNB rejected the email or password.
  - Check them by logging in to myTNB in a browser, and check that **View Energy Usage** opens the smartliving dashboard.
  - After an auth error the app waits one full sync interval before trying again, to avoid getting your account locked.
- **TNB `meter_error` (Smart meter not found):** the login worked, but no meter was returned for this account. Log in at myaccount.mytnb.com.my, open **View Energy Usage** on the account, copy the address (it contains `/AccountManagement/SmartMeter/Index/TRIL?caNo=...`) and paste it as the account's `smartmeter_url`. The app retries every 3 hours.
- **Debugging a myTNB login:** turn on **Verbose login log** in the panel's TNB card. The next sync logs each step (`[login]`, `[sso]`, `[smart meter page]`, `[dashboard n]` …). The log is safe to share. Turn the switch off again afterwards.
- **SAJ `not_found` (Account not found):** compare the number with the bill (8 digits, a letter, 7 digits) and try it at ranhillsaj.com.my → **Pay Bills**. The app checks again after a day, or when you press **Check now**.
- **SAJ `layout_changed` (Site changed):** the page no longer looks the way the app expects. It is saved to `/data/last_unrecognised_page.html` inside the app (it may contain your name and address). Please open an issue, without attaching the page unless you have removed your details.
- **SAJ `error` (Site error):** the site was slow, down or blocked the request. It usually clears on its own.
- **No entities:** check **Entities via** in the panel. With MQTT, make sure the Mosquitto broker and the MQTT integration are running. Without MQTT, the entities come back within 5 minutes of a Home Assistant restart.
- **TNB estimate is too low after a restart:** energy used while the app was stopped is still counted when it comes back, because readings are cumulative. A meter reset (the reading going backwards) is not counted.
- **Icons missing in the app store:** open **⋮ → Check for updates** in the app store. Home Assistant caches app icons per repository refresh.
- **Privacy:** passwords, cookies, tokens and smart meter URLs are never logged. The panel, logs and entities show account numbers masked (first and last 4 characters) and only the initials of a water account holder's name. The address on the SAJ Quick Payment page is never read or stored.

## Credits

Inspired by:
- [feiming/homeassistant-MyTNB](https://github.com/feiming/homeassistant-MyTNB), for the myTNB login flow and statistics import
- [salihinsaealal/home-assistant-tnb-calculator](https://github.com/salihinsaealal/home-assistant-tnb-calculator), for the tariff, ToU, holidays, spike guard and predictions
- [zubir2k/HomeAssistantTNB](https://github.com/zubir2k/HomeAssistantTNB), the original bill template

Not affiliated with Tenaga Nasional Berhad, Ranhill SAJ Sdn. Bhd. or Telekom Malaysia.
