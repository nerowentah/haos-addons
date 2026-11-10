# Changelog

## 0.1.0

- First release. **TNB Usage Checker (0.4.1) and Ranhill SAJ Bill Checker (0.2.1) in one app**: one container, one sidebar panel (**Utility Bills**) and one MQTT connection. Every TNB and SAJ feature carries over unchanged.
- **Options per provider:** `tnb_accounts` plus the `tnb_` settings (the old TNB options with a prefix), and `saj_accounts`. `notify_on_failure`, `notify_on_unpaid`, `mqtt` and `log_level` are shared. A provider with no accounts does nothing, and the app starts with no accounts at all.
- **Entities keep their ids and history.** MQTT unique ids, entity ids, state topics and the Energy dashboard statistics (`power_tnb:usage_<id>`, `power_tnb:cost_<id>`) are the same as in the old apps. The MQTT status topic is now `malaysia_utility_bills/status`.
- **Moving from the old apps:**
  - **Import** buttons copy an old app's accounts and options, with `!secret` references kept as references.
  - While an old app is still running, its provider waits (**Waiting for … to stop**), so the two apps never write the same entities. It takes over within 5 minutes of the old app stopping.
- Not carried over: the TNB `notification_overrides` migration and the SAJ `poll_interval_hours` clean-up, both for options that only existed in older versions of the old apps.
