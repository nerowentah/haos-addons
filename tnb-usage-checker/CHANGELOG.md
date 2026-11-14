# Changelog

## 0.5.5

- **Code scanning:** the start-up line that lists each account (label, tariff, billing day, data sources) is marked as reviewed. It never contained a password or an account number; the scanner flagged it because the same record also holds the myTNB password. No functional change.

## 0.5.4

- **Passwords and keys are wrapped as secrets.** The myTNB password and the Calendarific API key are only revealed in the request that needs them; they cannot show up in a log line or a debug dump. The MQTT password is masked in logs too.
- **Stricter internal checks.** The app's code now passes strict type checking: options, panel requests, the saved state and the MQTT broker details are validated where they enter the app. No visible change; entity ids, MQTT topics, statistics ids and the saved state keep their format.
- **Fix:** an unexpected holiday list from Calendarific, or MQTT broker details with an unusable port, are skipped or reported in the log instead of stopping the app.

## 0.5.3

- **Shared panel update (security):** the panel only accepts changes that come from its own page (a request from another web site is refused), every page it serves carries a Content-Security-Policy and may only be shown inside Home Assistant, and stalled or oversized requests are dropped. No visible change in this app.
- **State files are private.** Everything the app saves in `/data` is written atomically and readable by the app only.
- **The Supervisor token is masked** if it ever reaches a log line.
- **Panel:** the Energy statistics row shows its icon again.
- **Outbound requests** (Calendarific holidays) refuse redirects to plain http and oversized answers.

## 0.5.2

- **Shared panel update:** buttons can now upload files (SSL Cert Manager uses this to import certificates); the panel code stays identical across this repository's apps. No visible change in this app.

## 0.5.1

- **Panel:** can open external pages (e.g. a sign-in) in a new browser tab; the shared panel code is kept identical across this repository's apps. No visible change in this app.

## 0.5.0

- **The Energy dashboard import is now visible.** Each account card shows **Energy import** (`ok`, `failed`, `off` or waiting for the first sync) and **Last import**, lists the two statistic ids to pick in the dashboard, and has a button that opens **Settings → Dashboards → Energy**. Two new diagnostic entities carry the same state: `sensor.tnb_<id>_stats_status` and `sensor.tnb_<id>_stats_last_import`. Nothing needs reconfiguring: the statistic ids and the existing entities are unchanged.
- **Safer re-imports.** The baseline lookup now checks that the statistic exists before querying (no more "day since 2015" query for a new account), rejects a malformed recorder reply instead of silently restarting the cumulative sum at zero, and stops one second before the local midnight when it falls back to daily statistics, so the first day of a re-imported window is no longer counted twice.
- **The statistics import has tests.** Baseline lookup, metadata fallbacks, the websocket handshake and the statistic ids are covered, along with the panel and diagnostic entities.
- **Startup warning removed.** The service is now registered in the `user` bundle under `/etc/s6-overlay/user-bundles.d` instead of the deprecated `/etc/s6-overlay/s6-rc.d/user`, so s6-overlay no longer prints `defining user bundles in /etc/s6-overlay/s6-rc.d is deprecated`. No functional change.
- **Config cleanup.** Removed options that already match Home Assistant defaults, so the app passes the community app config linter. No behaviour change.

## 0.4.3

- **Deprecation withdrawn — this app stays.** Malaysia Utility Bills is retired after a short trial; one app per utility provider is back, since not every user has the same providers. This app continues as the TNB app with no functional changes from 0.4.1. If you had moved to Malaysia Utility Bills, uninstall it and reinstall this app: entity ids, history and Energy dashboard statistics are identical, so nothing is lost.

## 0.4.2

- **Replaced by Malaysia Utility Bills.** The new app handles TNB and Ranhill SAJ together and keeps this app's entities, history and Energy dashboard statistics. Install it, press **Import from TNB Usage Checker** in its panel, then stop this app. This app keeps working but gets no new features. It logs a reminder on start and shows one notification.
- *Withdrawn:* the successor app was retired in 0.4.3; this app is the TNB app again.

## 0.4.1

- Shared panel update: buttons can ask for a line of text, and cards can list plain rows without a button (used by API Key Manager). This app's panel looks the same.

## 0.4.0

- **`!secret` works with live changes.** Options that use `!secret name` are now resolved when you edit them in the Configuration tab or flip a panel switch. Before, the app read the literal `!secret …` text as the value. A panel switch now writes the reference back, never the secret value, and does nothing if Home Assistant can't be reached. A new secret needs an app restart. See *Passwords and secrets* in the docs.
- **Amount due from myTNB.** Every sync reads the bill card on the myTNB account dashboard (*My Amount Due*, *I've cleared all bills*, or *I've paid extra*, which is a credit and shows as a negative amount). With several accounts on one login, the app picks this account in the dashboard's account list first. Prepaid accounts are skipped. New entities per myTNB account: **Amount due** (RM), **Due date** and a **Payment due** binary sensor (`binary_sensor.tnb_<account>_payment_due`). The account card shows the amount and *Due by …* or *Paid*. If the bill can't be read, the sync still succeeds and a warning is logged.
- **Unpaid bill notifications** (`notify_on_unpaid`, on by default, also a panel switch). You get one notice when a new amount is due, a reminder once a week while it stays unpaid, and one when it is paid. Notices show the account name, amount and due date, never the account number. The reminder clock survives restarts.
- **Log level** (`log_level`, default `info`): `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. `debug` also shows the myTNB login trace. It applies without a restart.
- `portal_interval_hours` now goes up to 168 (7 days).
- The CLI smoke test also prints the amount due. Its documented `python3 -I` form could not import its own modules; the `-I` flag is dropped.

## 0.3.5

- **myTNB maintenance windows** (`maintenance_windows`, default `daily 00:00-06:00`, Malaysia time).
  - No myTNB calls inside a window. A sync due then runs a few random minutes after the window ends.
  - **Sync myTNB now** says when the window ends. Accepts `daily`, `weekdays`, `weekends`, day names, ranges and lists, and windows that cross midnight.
  - The panel's Settings card shows the windows and whether one is active.
- **Back-off when myTNB is down for maintenance.** An HTTP 503 or a maintenance page gives the new `maintenance` status (**TNB maintenance**). The last data is kept and no failure alert is sent. Retries come after 30 min, doubling up to 6 h, and the back-off resets after the next successful sync.

## 0.3.4

- **Redesigned account cards.**
  - The cycle's cost and kWh are shown as large figures, with a billing-cycle progress bar ("Day 17 of 31 · 15 days left · next cycle 23 Nov").
  - A bar chart shows the last 14 complete days of myTNB usage. The newest day's figure and trend are written under it, and hovering a bar shows its value.
  - Details are compact: data up to, last sync and account number.
  - Accounts with an energy sensor lead with the bill estimate.
  - The switches fold into a **Settings** row that remembers whether it is open.
- The daily chart is kept in `/data`, so it shows right after a restart.

## 0.3.3

- **Daily usage report** (`daily_report`, on by default, also a panel switch). When a sync brings in a new complete day, each account sends one notification with that day's kWh and RM, the change from the day before, and the billing cycle so far. For example: *"Home: 8.42 kWh, RM 3.71 (▲12% vs the day before). Cycle since 23 Oct: 248.99 kWh, RM 110.62."* It is sent once per day, even across restarts, and never replaces a sync failure alert.
- **Clearer smart meter errors.** If login works but the smartliving dashboard has no meter id, the status is now `meter_error` (**Smart meter not found**) instead of `auth_error`. The log says where the dashboard landed and how many smart meter links the account page listed. It retries after 3 hours instead of a full sync interval. The phone notification points to the fix instead of the password.
- **Fixed myTNB sync.** Verified live on two accounts, now about 4 seconds per account. It had been failing with "did not return a meter id" or redirect loops. There were three causes:
  - The app now completes the smartliving hand-off a browser does. The **Loading Smart Meter** page posts a session token to smartliving's `/login?renew`, and the app skipped that step.
  - Each portal host now uses one kept-alive connection, as a browser does. smartliving keeps sessions per backend, so a new connection per request lost the session and redirected through `/login` until it gave up.
  - Requests use browser header casing and order. Cloudflare in front of smartliving answered HTTP 403 to Python's default `User-agent` and `Dnt` spellings.
- **A smartliving redirect loop is no longer reported as a login failure.** `auth_error` is now only for a rejected email or password, and waits a full interval before retrying. Session hiccups show `error` and retry after 30 minutes, with a notification after 3 failures in a row.
- **Verbose login log** (`debug_login`, or the switch in the panel's Settings card). It logs each myTNB step, including every redirect hop, with HTTP status, page titles, and cookie and form field names. Values are never logged: passwords, cookies, form values and account numbers (only the last 4 characters) stay out of the log. It applies without a restart. The CLI smoke test also accepts `MYTNB_DEBUG=1`.
- **`smartmeter_url` is now a masked field** in the Configuration tab, like the passwords, because its `caNo` identifies the account.
- **`smartmeter_url` wins when set**, and its `caNo` is used exactly as copied, so encoded values work too. Use it when the link built from `account_number` does not reach the meter: open **View Energy Usage** for the account on the myTNB website and paste that address.

## 0.3.2

- Lighter and quieter:
  - Without an MQTT broker, entity states are only sent to Home Assistant when they change. They were re-sent on every meter poll before, and in testing 10 unchanged polls went from 150 HTTP calls to 0. A full refresh still runs every 5 minutes.
  - With MQTT, unchanged retained payloads are skipped. Everything is re-sent after a reconnect or a Home Assistant restart.
  - The MQTT client and websocket libraries are only loaded when needed. Start-up loads about 15% less Python memory and about 25% faster.
  - The main loop sleeps until the next job (up to 60 s) instead of waking every 10 s.
- The panel stops polling while it is hidden (another tab, or the sidebar panel closed).
- Python bytecode is precompiled in the image, for a faster start with nothing written at runtime.

## 0.3.1

- Examples in the setup page and docs use obvious placeholder account numbers and names.

## 0.3.0

- **One login, many accounts.** Set `mytnb_username` and `mytnb_password` once at the top, then give each account its `account_number` (the 12-digit number on the bill). The smart-meter link is built automatically, so there's no more pasting `smartmeter_url`. An account can still use its own login, and `smartmeter_url` keeps working as a legacy option.
- **No more crash loop on a bad configuration.** The app stays up and the panel shows exactly what to fix, with an example. It starts by itself within 15 seconds once the options are valid.
- **The panel** shows each account's masked number and which login it uses (masked).
- **Runs as a proper s6-overlay service.** The `legacy-cont-init` / `legacy-services` / `s6-rc.d is empty` log noise is gone, and a crash now stops the app cleanly so the Supervisor watchdog can restart it.
- **Icons** on sections, cards, switches and buttons in the panel. The store icon is now 128×128 and the logo 250×100, with the new name.

## 0.2.1

- Renamed from **Power TNB** to **TNB Usage Checker** (sidebar: **TNB Usage**). The repository folder is now `tnb-usage-checker/`. The slug, image, entity ids and Energy-dashboard statistics are unchanged, so there is nothing to reconfigure.
- The panel can link straight to Home Assistant settings pages and ask for confirmation before an action (shared panel update).

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
