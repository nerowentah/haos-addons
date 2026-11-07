# Changelog

## 0.2.0

- MQTT is optional. The app no longer crash-loops when no broker exists (Supervisor answered HTTP 400 for `/services/mqtt`). It checks for a broker every minute and switches to MQTT discovery as soon as one appears, without a restart.
- New `mqtt` switch (panel and Configuration tab). Off, or without a broker, the same `sensor.tnb_*` entities are created through the Home Assistant states API. The panel's Settings card shows which path is in use.
- Phones are no longer typed into the Configuration tab: the app lists the Companion apps linked to Home Assistant automatically (at start and every 5 minutes). Per-phone switches live in the panel and in `/data`; old `notification_overrides` choices are migrated once and the option is removed.
- Log lines use local time from the first line.

## 0.1.0

- Initial release.
- Multiple TNB accounts. Each account uses a myTNB portal login, a local energy sensor, or both.
- myTNB smartliving import, ported from feiming/homeassistant-MyTNB. Hourly usage and daily cost become `power_tnb:usage_<account>` and `power_tnb:cost_<account>` long-term statistics for the Energy dashboard. Each sync re-imports the 30-day window so data TNB publishes late is filled in.
- Live bill estimate on the 2025 domestic tariff: General and ToU, AFA, EEI, KWTBB, SST, NEM carry-forward. Includes a prediction for the end of the billing cycle and per-account billing start days.
- ToU peak/off-peak split with built-in fixed-date holidays, optional Calendarific holidays and a manual holiday list.
- MQTT discovery: one device per account, retained discovery, an availability topic, and automatic removal of entities that no longer apply.
- Notifications when a sync fails and when it recovers, with a switch per phone (all on by default).
- Sidebar panel (ingress) styled like Home Assistant: account cards with live figures, switches and **Sync myTNB now**. Switches (`enabled`, `portal_enabled`, `meter_enabled`, `statistics_enabled`, notifications, phones) apply immediately and are saved to the options. Configuration-tab edits are hot-reloaded without a restart.
