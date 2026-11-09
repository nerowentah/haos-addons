# Changelog

## 0.1.0

- Initial release.
- Lists the long-lived access tokens (API keys) of every Home Assistant user, read-only from the auth store. Token secrets are dropped while parsing.
- Each key shows its description, owner, created, last used, last IP and expiry.
- Flags: deactivated owner, expired, never used, unused (`unused_days`), vague description (`vague_words`), duplicate description, admin rights.
- Review per key: **Approve**, **Suspicious**, **Reset** and a note, kept in `/data`. Approved keys can be hidden.
- **How to revoke** guidance. The app never changes Home Assistant's accounts or tokens.
- Optional list of each user's login sessions.
- Notifications for new keys and read failures, with a switch per phone. Phones are found automatically.
