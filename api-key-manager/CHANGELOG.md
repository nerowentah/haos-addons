# Changelog

## 0.1.4

- **Shared panel update (security):** the panel only accepts changes that come from its own page (a request from another web site is refused), every page it serves carries a Content-Security-Policy and may only be shown inside Home Assistant, and stalled or oversized requests are dropped. No visible change in this app.
- **State files are private.** Everything the app saves in `/data` is written atomically and readable by the app only.
- **The Supervisor token is masked** if it ever reaches a log line.

## 0.1.3

- **Shared panel update:** buttons can now upload files (SSL Cert Manager uses this to import certificates); the panel code stays identical across this repository's apps. No visible change in this app.

## 0.1.2

- **Panel:** can open external pages (e.g. a sign-in) in a new browser tab; the shared panel code is kept identical across this repository's apps. No visible change in this app.

## 0.1.1

- **Startup warning removed.** The service is now registered in the `user` bundle under `/etc/s6-overlay/user-bundles.d` instead of the deprecated `/etc/s6-overlay/s6-rc.d/user`, so s6-overlay no longer prints `defining user bundles in /etc/s6-overlay/s6-rc.d is deprecated`. No functional change.
- **Config cleanup.** Removed options that already match Home Assistant defaults, so the app passes the community app config linter. No behaviour change.

## 0.1.0

- Initial release.
- Lists the long-lived access tokens (API keys) of every Home Assistant user, read-only from the auth store. Token secrets are dropped while parsing.
- Each key shows its description, owner, created, last used, last IP and expiry.
- Flags: deactivated owner, expired, never used, unused (`unused_days`), vague description (`vague_words`), duplicate description, admin rights.
- Review per key: **Approve**, **Suspicious**, **Reset** and a note, kept in `/data`. Approved keys can be hidden.
- **How to revoke** guidance. The app never changes Home Assistant's accounts or tokens.
- Optional list of each user's login sessions.
- Notifications for new keys and read failures, with a switch per phone. Phones are found automatically.
