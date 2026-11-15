# Changelog

## 0.1.0

First release.

- **Overview:** free, used and total disk space, a 30-day chart of free space, and a warning (with a notification) when free space drops below a limit you set.
- **Where the space is:** the eight areas the Supervisor reports, app data per app, and the configuration folder per folder.
- **Recorder database:** **Purge** and **Purge and repack**. The result comes from watching the size of the database, because a repack can outlast the five minutes Home Assistant's answer is waited for.
- **Backups:** delete selected local backups that are not password-protected. A backup that also has a copy elsewhere keeps that copy.
- **Logs and caches:** empties `home-assistant.log` and deletes rotated logs and the text-to-speech cache. The card only appears when there is something to clean.
- **Automatic clean-up is off by default.** When switched on it runs once a day: logs and caches, and a recorder purge without a repack. Backups are only deleted when **Also delete old backups** is switched on as well.
- **Never touched:** the operating system, Docker, the host journal, other apps' data, `.storage`, `deps`, `custom_components` and the database files.
