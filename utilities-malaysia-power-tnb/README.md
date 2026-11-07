# Power TNB

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

A Home Assistant app (add-on) for Malaysian **TNB** electricity that supports **multiple accounts**. For each account it can:

- **Import myTNB smart meter data.** It logs in to the myTNB portal and pulls hourly usage (kWh) and cost (RM). These become long-term statistics you can pick in the **Energy dashboard**.
- **Estimate the running bill live** from a local energy sensor such as a Shelly EM or CT clamp. It uses the 2025 TNB domestic tariff: General or Time-of-Use, capacity, network, retail, AFA, EEI, KWTBB, SST and NEM export credit.

Each account appears as its own device, created through MQTT discovery.

Inspired by:

- <https://github.com/feiming/homeassistant-MyTNB>, for the myTNB smartliving login flow and statistics import
- <https://github.com/salihinsaealal/home-assistant-tnb-calculator>, for the tariff calculator, ToU and holiday handling, spike guard and predictions
- <https://github.com/zubir2k/HomeAssistantTNB>, the original TNB bill template

> **Disclaimer:** myTNB has no public API. The portal flow is unofficial and may break if TNB changes its website. Bill estimates are indicative only. Your TNB bill is authoritative.

## Features

- **Panel in the Home Assistant sidebar** (**Power TNB**, opened through ingress). Each account gets a card showing:
  - the bill estimate and predicted bill
  - usage for the cycle and for today
  - the peak/off-peak split
  - myTNB cycle usage and cost, data freshness and sync status
  - switches and a **Sync myTNB now** button

  It follows your Home Assistant theme, including dark mode, and works on phones.
- **Switches take effect immediately.** Each account has switches for **Account**, **myTNB sync**, **Energy dashboard import** and **Live bill estimate**. There are also switches for sync-failure notifications and for each phone. Every switch is saved into the app options, so it also shows in the Configuration tab. Configuration-tab edits are hot-reloaded within about 30 seconds, with no restart. A switched-off source keeps its entities, which show as *unavailable* instead of being deleted.
- **Multiple accounts.** Each account can use myTNB credentials, an energy sensor, or both.
- **Energy dashboard statistics.** Each account gets `power_tnb:usage_<account>` (kWh) and `power_tnb:cost_<account>` (MYR). Every sync re-imports the last 30 days, so hours that TNB publishes late get filled in instead of being skipped.
- **Live bill estimate** with a full breakdown in the attributes. There is also a predicted end-of-cycle bill, which blends the current trend with the average of the last 3 cycles.
- **Billing cycles** follow each account's bill start day. Finished cycles are archived (the last 12 are kept) and NEM excess is carried forward.
- **ToU support.**
  - Weekdays 14:00-22:00 count as peak. Readings that cross a boundary are split by time.
  - Weekends and public holidays count as off-peak. Fixed-date holidays are built in. Lunar holidays come from an optional Calendarific key or the manual `extra_holidays` list.
- **Energy sensor safeguards.** A meter that resets is re-baselined automatically. Unrealistic jumps (more than 10 kWh per 5 minutes) are ignored.
- **Phone notifications** when a myTNB sync fails and again when it recovers. Each phone has its own switch, all on by default, the same as the LG webOS app in this repository.
- **Privacy.** Passwords, cookies, tokens and smart meter URLs (which contain your account number) are never logged.

## Requirements

- Home Assistant OS or Supervised.
- The **Mosquitto broker** app, with the MQTT integration set up. This app requests the broker credentials automatically (`mqtt:need`).
- For myTNB data: a smart meter (AMI) and a myTNB login.

## Installation

1. Go to **Settings → Apps → App Store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **Power TNB**, configure your accounts (see below), then **Start** it.

## Configuration

```yaml
portal_interval_hours: 6
meter_poll_seconds: 60
afa_rate: -0.0047           # RM/kWh; update monthly from TNB/ST announcements
calendarific_api_key: ""    # optional
extra_holidays:
  - "2026-03-21"            # e.g. Hari Raya Aidilfitri
notify_on_failure: true
notification_overrides: []  # filled in automatically with your phones
accounts:
  - name: "Home"
    tariff: "tou"
    billing_start_day: 12
    mytnb_username: "you@example.com"
    mytnb_password: "********"
    smartmeter_url: "https://myaccount.mytnb.com.my/AccountManagement/SmartMeter/Index/TRIL?caNo=1234..."
    import_entity: "sensor.shelly_em_channel_1_energy"
    export_entity: "sensor.solar_export_energy"
  - name: "Parents"
    tariff: "general"
    billing_start_day: 1
    mytnb_username: "parents@example.com"
    mytnb_password: "********"
    smartmeter_url: "https://myaccount.mytnb.com.my/AccountManagement/SmartMeter/Index/TRIL?caNo=2201..."
```

### Global options

| Option | Default | Description |
| --- | --- | --- |
| `portal_interval_hours` | `6` | Hours between myTNB syncs (1-24). TNB data is about 2 days behind. |
| `meter_poll_seconds` | `60` | How often each account's energy sensor is read (10-600). |
| `afa_rate` | `0.0` | Automatic Fuel Adjustment in RM/kWh. A negative value is a rebate. It only applies above 600 kWh. |
| `calendarific_api_key` | `""` | Optional key for lunar public holidays (ToU off-peak). |
| `extra_holidays` | `[]` | Extra `YYYY-MM-DD` dates treated as off-peak all day. |
| `notify_on_failure` | `true` | Master switch for sync failure and recovery notifications. |
| `notification_overrides` | `[]` | One row per discovered mobile app, each on by default. This list is kept up to date automatically. |

### Account options

| Option | Required | Description |
| --- | --- | --- |
| `name` | yes | Friendly name. It also sets the ids: `Home` gives the device `TNB Home` and entities `sensor.tnb_home_*`. Names must be unique. |
| `tariff` | yes | `general` or `tou`. |
| `billing_start_day` | yes | Day of the month your billing cycle starts (1-31). Short months use their last day. |
| `mytnb_username`, `mytnb_password`, `smartmeter_url` | together | myTNB login. To get the URL, open the account in myTNB, click **View Energy Usage** and copy the address. It must contain `/AccountManagement/SmartMeter/Index/TRIL?caNo=`. |
| `import_entity` | optional | Cumulative grid-import energy sensor (kWh, Wh or MWh). Enables the live estimate. |
| `export_entity` | optional | Cumulative grid-export energy sensor, for NEM. |
| `enabled`, `portal_enabled`, `meter_enabled`, `statistics_enabled` | optional | On/off switches, all on by default. These are the panel switches. Changes apply without a restart. |

Every account needs the myTNB login, an `import_entity`, or both.

## Entities

Each account gets the device **TNB &lt;name&gt;**.

**From myTNB** (if credentials are set):

| Entity | Description |
| --- | --- |
| `sensor.tnb_<id>_portal_usage_cycle` / `_portal_cost_cycle` | kWh and RM so far in the current billing cycle, according to TNB |
| `sensor.tnb_<id>_portal_usage_latest_day` / `_portal_cost_latest_day` | The most recent day TNB has published |
| `sensor.tnb_<id>_portal_data_as_of`, `_portal_last_sync`, `_portal_status` | Diagnostics |

**From the energy sensor** (if `import_entity` is set):

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

There is also one shared entity: `binary_sensor.tnb_holiday_today`.

## Energy dashboard

1. Open **Settings → Dashboards → Energy → Electricity grid → Add consumption**.
2. Select the statistic **TNB &lt;name&gt; usage**.
3. Under cost, choose **Use an entity tracking the total costs** and select **TNB &lt;name&gt; cost**.

The statistics appear after the first successful sync.

## Tariff used

TNB domestic tariff for Peninsular Malaysia, valid 1 July 2025 to 31 December 2027:

| Item | Rate |
| --- | --- |
| Energy, General | 27.03 sen/kWh. Above 1500 kWh the whole bill is charged at 37.03 sen. |
| Energy, ToU | Peak 28.52 / off-peak 24.43 sen. Above 1500 kWh: 38.52 / 34.43 sen. |
| Capacity / Network | 4.55 / 12.85 sen/kWh |
| Retail | RM10 per month, waived at 600 kWh or less |
| AFA | `afa_rate` × kWh, only above 600 kWh |
| EEI (Energy Efficiency Incentive) | Rebate on the whole bill, from 25 sen/kWh (≤200 kWh) sliding down to 0.5 sen/kWh (≤1000 kWh) |
| KWTBB | 1.6% when above 300 kWh |
| SST | 8% on the share of usage above 600 kWh, plus the retail charge |
| NEM | Exports are credited at the off-peak energy rate plus capacity plus network, up to the import charges. Any excess is carried forward. |

The rates live in `rootfs/app/tariff.py`.

## Troubleshooting

- **`auth_error` status:**
  - Check the email and password by logging in to myTNB in a browser.
  - Check that the account has a smart meter and that **View Energy Usage** opens the smartliving dashboard.
  - After an auth error the app waits one full sync interval before trying again, to avoid getting your account locked.
- **No entities:** make sure the Mosquitto broker and the MQTT integration are running. Discovery is published again whenever Home Assistant restarts.
- **Estimate is too low after a restart:** energy used while the app was stopped is still counted when it comes back, because readings are cumulative. A meter reset (the reading going backwards) is not counted.

## License

MIT. See the repository. [Changelog](CHANGELOG.md)
