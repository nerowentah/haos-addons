# Changelog

## 0.1.0

- First release.
- **Amount due for one or more Ranhill SAJ water accounts**, read from the public Quick Payment page. No login is needed, and the app never starts a payment.
- **Entities per account:** amount due (RM), when it last changed, check status and last checked. With the Mosquitto broker app they form one MQTT device per account. Without it they are plain entities with the same ids.
- **Sidebar panel (SAJ Water):** one card per account with a **Check now** button and an on/off switch, plus settings and per-phone notification switches.
- **Notifications** when an account number is not recognised, after 3 site errors in a row, and once on recovery.
- **No crash loop on a bad configuration.** The panel shows what to fix, and the app starts by itself once the options are valid.
- **Privacy:** account numbers are masked and only the holder's initials are shown. The address is never read.
