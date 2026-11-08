# Changelog

## 0.2.0

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
