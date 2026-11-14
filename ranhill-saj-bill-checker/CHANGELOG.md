# Changelog

## 0.2.8

- **Stricter internal checks.** The app's code now passes strict type checking: options, panel requests, the saved state and the MQTT broker details are validated where they enter the app. No visible change; entity ids, MQTT topics and the saved state keep their format.
- **Fix:** damaged values in the saved state (for example an amount that is not a number) are ignored instead of stopping the panel or the next check.
- **Fix:** MQTT broker details with an unusable port are reported in the log and the app keeps using what it had, instead of stopping.
- **The MQTT password is masked** if it ever reaches a log line.

## 0.2.7

- **Shared panel update (security):** the panel only accepts changes that come from its own page (a request from another web site is refused), every page it serves carries a Content-Security-Policy and may only be shown inside Home Assistant, and stalled or oversized requests are dropped. No visible change in this app.
- **State files are private.** Everything the app saves in `/data` is written atomically and readable by the app only.
- **The Supervisor token is masked** if it ever reaches a log line.
- **The saved copy of an unrecognised page** (it may contain your name and address) is readable by the app only.

## 0.2.6

- **Shared panel update:** buttons can now upload files (SSL Cert Manager uses this to import certificates); the panel code stays identical across this repository's apps. No visible change in this app.

## 0.2.5

- **Panel:** can open external pages (e.g. a sign-in) in a new browser tab; the shared panel code is kept identical across this repository's apps. No visible change in this app.

## 0.2.4

- **Startup warning removed.** The service is now registered in the `user` bundle under `/etc/s6-overlay/user-bundles.d` instead of the deprecated `/etc/s6-overlay/s6-rc.d/user`, so s6-overlay no longer prints `defining user bundles in /etc/s6-overlay/s6-rc.d is deprecated`. No functional change.
- **Config cleanup.** Removed options that already match Home Assistant defaults, so the app passes the community app config linter. No behaviour change.

## 0.2.3

- **Deprecation withdrawn — this app stays.** Malaysia Utility Bills is retired after a short trial; one app per utility provider is back, since not every user has the same providers. This app continues as the Ranhill SAJ app with no functional changes from 0.2.1. If you had moved to Malaysia Utility Bills, uninstall it and reinstall this app: entity ids and history are identical, so nothing is lost.

## 0.2.2

- **Replaced by Malaysia Utility Bills.** The new app handles TNB and Ranhill SAJ together and keeps this app's entities and their history. Install it, press **Import from Ranhill SAJ Bill Checker** in its panel, then stop this app. This app keeps working but gets no new features. It logs a reminder on start and shows one notification.
- *Withdrawn:* the successor app was retired in 0.2.3; this app is the Ranhill SAJ app again.

## 0.2.1

- Shared panel update: buttons can ask for a line of text, and cards can list plain rows without a button (used by API Key Manager). This app's panel looks the same.

## 0.2.0

- **`!secret` works with live changes.** Options that use `!secret name` are now resolved when you edit them in the Configuration tab or flip a panel switch. Before, the app read the literal `!secret …` text as the value. A panel switch now writes the reference back, never the secret value, and does nothing if Home Assistant can't be reached. A new secret needs an app restart. See *Passwords and secrets* in the docs.
- **Weekly checks.** The `poll_interval_hours` option is gone. Each account is checked once a week, and **Check now** works any time. The schedule continues from the last check after a restart. An unknown account number or a changed page is retried daily. The old option is removed from the saved configuration on start.
- **Payment due binary sensor** (`binary_sensor.saj_<account>_payment_due`). It is on while an amount is due, and the account card shows *Due* or *Paid*.
- **Unpaid bill notifications** (`notify_on_unpaid`, on by default, also a panel switch). You get one notice when an amount is due, a reminder at each weekly check while it stays unpaid, and one when it is paid.
- **Log level** (`log_level`, default `info`): `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. It applies without a restart.

## 0.1.0

- First release.
- **Amount due for one or more Ranhill SAJ water accounts**, read from the public Quick Payment page. No login is needed, and the app never starts a payment.
- **Entities per account:** amount due (RM), when it last changed, check status and last checked. With the Mosquitto broker app they form one MQTT device per account. Without it they are plain entities with the same ids.
- **Sidebar panel (SAJ Water):** one card per account with a **Check now** button and an on/off switch, plus settings and per-phone notification switches.
- **Notifications** when an account number is not recognised, after 3 site errors in a row, and once on recovery.
- **No crash loop on a bad configuration.** The panel shows what to fix, and the app starts by itself once the options are valid.
- **Privacy:** account numbers are masked and only the holder's initials are shown. The address is never read.
