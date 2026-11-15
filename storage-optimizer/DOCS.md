# HA Storage Optimizer

Home Assistant warns you when the disk is filling up, but not what is filling it. This app shows where the space is, down to each app and each folder of the configuration, and frees the parts that are safe to free: the recorder database, old local backups, and Home Assistant's log and cache files.

Nothing is cleaned unless you press a button or switch on the automatic clean-up.

## Installation

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **HA Storage Optimizer** and **Start** it. No configuration is needed.
3. Open **Storage Optimizer** from the sidebar. Only administrators see it.

The first scan starts right away. On a large disk it takes a minute or two.

## Configuration

```yaml
auto_clean: false
auto_clean_backups: false
keep_backups: 3
low_space_percent: 10
notify_on_low_space: true
```

| Option | Default | Description |
| --- | --- | --- |
| `auto_clean` | `false` | Once a day: clean the log and cache files and purge the recorder (without a repack). |
| `auto_clean_backups` | `false` | Only with `auto_clean` on: also delete old local backups that are not password-protected, keeping the newest `keep_backups`. |
| `keep_backups` | `3` | How many of the newest local, unprotected backups the automatic clean-up always keeps (1-50). |
| `low_space_percent` | `10` | The panel warns and a notification is sent when less than this share of the disk is free (1-50). |
| `notify_on_low_space` | `true` | Send the low-space notification. |
| `log_level` | `info` | How much is logged: `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. Applies without a restart. |

The three switches are also in the panel's **Settings** card. Changes made in the Configuration tab are picked up within about 30 seconds. You don't need to restart the app.

## Panel

### Overview

- **Free**, **Used** and **Total** for the disk Home Assistant runs on.
- A bar with the share used and when the disk was last scanned.
- **Free space, last 30 days:** one bar per day the app was running.
- **Scan now** scans again. Otherwise the disk is scanned every hour and after every clean-up.

The badge turns to **Low space** below `low_space_percent`.

### Where the space is

- **By area:** the eight areas the Supervisor reports, largest first: System, App data, App configuration, Media, Share, Backups, SSL and Home Assistant.
- **App data, per app:** how much each installed app keeps in its data folder.
- **Home Assistant, per folder:** the files and folders of the configuration folder, such as the recorder database, `custom_components` or `www`.

Each list shows the twelve largest entries and sums up the rest. Sizes are the sizes of the files, as on Home Assistant's own **Settings → System → Storage** page.

### Recorder database

Shows the size of `home-assistant_v2.db` and its write-ahead log.

- **Purge** asks Home Assistant to delete history older than the recorder's `purge_keep_days` (10 days unless you changed it). Long-term statistics are kept. The file usually stays the same size: the freed space is reused for new data.
- **Purge and repack** also rewrites the database so the file shrinks.

About the repack:
- Home Assistant needs free space about the size of the database while it works. On a nearly full disk it can fail.
- History is not recorded until it is done.
- A large database can take longer than the five minutes the Supervisor waits for Home Assistant's answer. The app does not rely on that answer. It watches the size of the database and reports the result in the **Activity** card once the size has stopped changing: how much was freed, or that nothing changed.

The app only sends the `recorder.purge` service call. It never opens, changes or deletes the database files. If the recorder uses MariaDB or PostgreSQL, the card says that no database file was found.

### Backups

Lists the backups that are stored on this system and are not password-protected. Each has a switch. **Delete selected** deletes the ones you switched on, after asking.

- Password-protected backups and backups stored only elsewhere (a network share, a cloud location) are not listed and cannot be deleted here. The card says how many there are.
- A backup that also has a copy elsewhere loses only its local copy.
- Before each delete the app asks the Supervisor for the backups again. A backup that has become protected or has gone in the meantime is skipped.
- The selection is not saved. It is cleared when the app restarts.

### Logs and caches

Shown only when there is something to clean. Home Assistant 2025.11 and later no longer write `home-assistant.log`, so on those versions the card usually does not appear.

**Clean** does this, and nothing else:

| File | What happens |
| --- | --- |
| `home-assistant.log`, `home-assistant.log.fault` | Emptied. Home Assistant keeps writing to them. |
| `home-assistant.log.1`, `home-assistant.log.old`, `OZW_Log.txt` | Deleted. |
| `tts/*.mp3`, `.wav`, `.ogg`, `.flac`, `.opus` | Deleted. Home Assistant makes a spoken message again the next time it is needed. |

### Not cleaned here

The areas the app shows but never changes: the system (operating system, Docker images and containers), other apps' data, your media, share and SSL files, and the host journal.

### Settings and Phones cards

- **Automatic clean-up:** see below.
- **Also delete old backups:** only available while automatic clean-up is on.
- **Notifications:** the low-space notification.
- **Phones** has one switch per Companion app linked to this Home Assistant. Phones are found automatically every 5 minutes, and new phones are switched on.

## Automatic clean-up

Off by default. When you switch it on, once a day the app:

1. cleans the log and cache files listed above;
2. asks Home Assistant to purge the recorder, without a repack.

It deletes backups only when **Also delete old backups** is on as well. Then it deletes the local, unprotected backups except the newest `keep_backups`. Password-protected backups, backups stored only elsewhere and backups without a readable date are never deleted automatically.

## What the app can and cannot touch

The app has the Supervisor's `manager` role, because the disk usage report and deleting a backup need it, and the configuration folder is mapped read-write, because emptying a log file needs it. What it does with that access is limited in the code:

- **Supervisor:** it reads disk usage, the list of backups and the names of the installed apps, and deletes backups. It does not start, stop, install or remove anything.
- **Home Assistant:** the only services it calls are `recorder.purge` and notifications.
- **Configuration folder:** it only changes the files in the table above. A file is refused when it is not on that list, is a symbolic link or sits behind one, leads outside the folder, or is not a regular file.
- **Never changed:** `.storage`, `deps`, `custom_components`, any database file (`*.db*`), the operating system, Docker, the host journal and other apps' data.

## Notifications

- **Low space** is reported once when free space drops below the limit, and again only after it has recovered and dropped again.
- Without any linked phones, the app creates a persistent notification instead.

## Privacy

- **The panel** shows sizes, folder names, app names and backup names. It is only available to administrators.
- **`/data/state.json`** holds the free-space history, the phone switches and the day of the last automatic clean-up.
- **The log** names the files that were cleaned and the ids of the backups that were deleted.
- **`!secret`** works for the options as in the other apps in this repository. A new secret takes effect after you restart the app.

## Troubleshooting

- **"The Supervisor did not report disk usage":** the report needs a recent Supervisor. Update Home Assistant OS and the Supervisor, then press **Scan now**. The clean-up cards still work.
- **The scan takes a long time:** the Supervisor walks every folder. A disk with very many small files can take a few minutes.
- **A repack reports that nothing changed:** there was nothing to free, or Home Assistant could not repack, usually because there was too little free space. Look for `recorder` messages in Home Assistant's log.
- **A backup is missing from the list:** it is password-protected, or it is stored only in another location.
- **The sizes don't add up to the disk's used space:** **System** is what is left after the other areas, so it includes Docker images and anything the Supervisor does not list separately.
