# Power TNB

Power TNB tracks Malaysian **TNB** electricity for one or more accounts. For each account it can:

- **Import myTNB smart meter data.** It logs in to the myTNB portal and pulls hourly usage (kWh) and cost (RM). These become long-term statistics you can pick in the **Energy dashboard**.
- **Estimate the running bill live** from a local energy sensor such as a Shelly EM or CT clamp. It uses the 2025 TNB domestic tariff: General or Time-of-Use, capacity, network, retail, AFA, EEI, KWTBB, SST and NEM export credit.

> **Disclaimer:** myTNB has no public API. The portal flow is unofficial and may break if TNB changes its website. Bill estimates are indicative only. Your TNB bill is authoritative.

## Requirements

- Home Assistant OS or Supervised.
- For myTNB data: a smart meter (AMI) and a myTNB login.
- For the live estimate: a cumulative grid-import energy sensor (kWh, Wh or MWh).
- Optional: the **Mosquitto broker** app and the MQTT integration. See [MQTT or not](#mqtt-or-not).

## Installation

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **Power TNB**.
3. Open the **Configuration** tab, set up your accounts (below), then **Start** the app.
4. Open **Power TNB** from the sidebar to see the panel.

## Configuration

```yaml
portal_interval_hours: 6
meter_poll_seconds: 60
afa_rate: -0.0047           # RM/kWh; update monthly from TNB/ST announcements
calendarific_api_key: ""    # optional
extra_holidays:
  - "2026-03-21"            # e.g. Hari Raya Aidilfitri
notify_on_failure: true
mqtt: true
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
| `mqtt` | `true` | Use MQTT discovery when a broker is found. See [MQTT or not](#mqtt-or-not). |

### Account options

| Option | Required | Description |
| --- | --- | --- |
| `name` | yes | Friendly name. It also sets the ids: `Home` gives the device `TNB Home` and entities `sensor.tnb_home_*`. Names must be unique. |
| `tariff` | yes | `general` or `tou`. |
| `billing_start_day` | yes | Day of the month your billing cycle starts (1-31). Short months use their last day. |
| `mytnb_username`, `mytnb_password`, `smartmeter_url` | together | myTNB login. To get the URL, open the account in myTNB, click **View Energy Usage** and copy the address. It must contain `/AccountManagement/SmartMeter/Index/TRIL?caNo=`. |
| `import_entity` | optional | Cumulative grid-import energy sensor (kWh, Wh or MWh). Enables the live estimate. |
| `export_entity` | optional | Cumulative grid-export energy sensor, for NEM. Needs `import_entity`. |
| `enabled`, `portal_enabled`, `meter_enabled`, `statistics_enabled` | optional | On/off switches, all on by default. These are the panel switches. |

Every account needs the myTNB login, an `import_entity`, or both.

Changes made in the Configuration tab are picked up within about 30 seconds. You don't need to restart the app.

## Panel

Open **Power TNB** from the sidebar, or click **Open web UI** on the app page. Only administrators see it. The panel follows your Home Assistant theme, including dark mode, and works on phones.

- **Account cards** show:
  - the bill estimate and predicted bill
  - usage for the cycle and for today, and the peak/off-peak split
  - myTNB cycle usage, cost, data freshness and sync status
  - a **Sync myTNB now** button
- **Account switches:**
  - **Account** turns the whole account on or off.
  - **myTNB sync** turns portal syncing on or off.
  - **Energy dashboard import** controls whether synced data becomes long-term statistics.
  - **Live bill estimate** turns energy-sensor polling on or off.

  A switched-off source keeps its entities. They show as *unavailable* instead of being deleted.
- **Settings card:**
  - AFA, today's holiday status and the sync intervals
  - **Entities via**, which shows whether MQTT or the states API is in use
  - the **Use MQTT when available** and **Sync failure notifications** switches
- **Phones card:** one switch per Companion app linked to this Home Assistant. Phones are found automatically at start and every 5 minutes. New phones are on, and phones that are no longer linked disappear. You never type phone names.

Account and settings switches are saved into the app options, so they also show in the Configuration tab. Phone switches are kept by the app itself. Every switch applies immediately.

## MQTT or not

| | With MQTT (Mosquitto broker app installed) | Without MQTT |
| --- | --- | --- |
| Entities | One **device** per account, entities can be renamed and assigned to areas | Plain entities with the same `sensor.tnb_*` ids, no device |
| After a Home Assistant restart | Come back immediately (retained discovery) | Come back within 5 minutes |
| Energy dashboard statistics | Yes | Yes (statistics don't depend on MQTT) |

- The app checks for a broker every minute.
- **Broker installed later:** the app switches to MQTT within a minute, with no restart. The states-API entities are removed first, so the entity ids stay the same.
- **MQTT switched off:** turn off **Use MQTT when available** in the panel, or set `mqtt: false`. The MQTT devices are removed and the plain entities take over.
- **Which mode is active:** the panel's **Entities via** shows it.

## Entities

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
| `sensor.tnb_<id>_predicted_bill` / `_predicted_kwh` | Projected bill and usage at the end of the cycle (current trend blended with the last 3 cycles) |
| `sensor.tnb_<id>_cycle_import` / `_cycle_peak` / `_cycle_offpeak` / `_cycle_export` | Billing-cycle kWh |
| `sensor.tnb_<id>_today_import` / `_today_export` | kWh today |
| `sensor.tnb_<id>_current_rate` | Approximate RM cost of the next kWh |
| `sensor.tnb_<id>_current_period` | `peak`, `off-peak` or `flat` |
| `sensor.tnb_<id>_tier` | `1` (≤1500 kWh) or `2` |
| `sensor.tnb_<id>_days_remaining`, `_nem_carry_kwh` | Diagnostics |

There is also one shared entity: `binary_sensor.tnb_holiday_today`.

**Billing cycles:**
- Each cycle follows the account's `billing_start_day`.
- Finished cycles are archived, and the last 12 are kept.
- NEM excess is carried forward.

**Energy sensor safeguards:**
- A meter that resets is re-baselined automatically.
- Unrealistic jumps (more than 10 kWh per 5 minutes) are ignored.

## Energy dashboard

1. Open **Settings → Dashboards → Energy → Electricity grid → Add consumption**.
2. Select the statistic **TNB &lt;name&gt; usage** (`power_tnb:usage_<id>`).
3. Under cost, choose **Use an entity tracking the total costs** and select **TNB &lt;name&gt; cost** (`power_tnb:cost_<id>`).

The statistics appear after the first successful sync. Every sync re-imports the last 30 days, so hours TNB publishes late are filled in rather than skipped.

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

**ToU:**
- Weekdays 14:00-22:00 count as peak. Readings that cross a boundary are split by time.
- Weekends and public holidays count as off-peak.
- Fixed-date holidays are built in. Lunar holidays come from `calendarific_api_key` or the `extra_holidays` list.

## Notifications

When a myTNB sync fails and when it recovers, the app notifies every phone that is switched on in the **Phones** card. It sends one notification per change, not one per retry. With no linked phones, it creates a persistent notification instead.

## Troubleshooting

- **`auth_error` status:**
  - Check the email and password by logging in to myTNB in a browser.
  - Check that the account has a smart meter and that **View Energy Usage** opens the smartliving dashboard.
  - After an auth error the app waits one full sync interval before trying again, to avoid getting your account locked.
- **No entities:**
  - Check **Entities via** in the panel.
  - **With MQTT:** make sure the Mosquitto broker and the MQTT integration are running.
  - **Without MQTT:** the entities come back within 5 minutes of a Home Assistant restart.
- **Estimate is too low after a restart:** energy used while the app was stopped is still counted when it comes back, because readings are cumulative. A meter reset (the reading going backwards) is not counted.
- **Privacy:** passwords, cookies, tokens and smart meter URLs (which contain your account number) are never logged. The panel never shows them.

## Credits

Inspired by:
- [feiming/homeassistant-MyTNB](https://github.com/feiming/homeassistant-MyTNB), for the myTNB login flow and statistics import
- [salihinsaealal/home-assistant-tnb-calculator](https://github.com/salihinsaealal/home-assistant-tnb-calculator), for the tariff, ToU, holidays, spike guard and predictions
- [zubir2k/HomeAssistantTNB](https://github.com/zubir2k/HomeAssistantTNB), the original bill template

Not affiliated with Tenaga Nasional Berhad.
