# API Key Manager

A **long-lived access token** is Home Assistant's API key. Anyone can create one under **Profile → Security**, and it works for 10 years with the full rights of its user. Home Assistant only shows you your own keys, so an administrator can't see what keys other people have made. This app lists every user's keys in one panel and lets you review each one.

## Installation

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **API Key Manager** and **Start** it. No configuration is needed.
3. Open **API Keys** from the sidebar. Only administrators see it.

## Configuration

```yaml
unused_days: 90
notify_on_new_keys: true
vague_words: []
```

| Option | Default | Description |
| --- | --- | --- |
| `unused_days` | `90` | A key that hasn't been used for this many days is flagged **Unused** (7-3650). |
| `notify_on_new_keys` | `true` | Notify when a new key is created, and when the keys can't be read 3 times in a row (and again when it recovers). |
| `vague_words` | `[]` | Extra words that make a description vague when it contains nothing else, on top of the built-in list (`test`, `token`, `key`, `api`, `temp`, `new` and others). |
| `log_level` | `info` | How much is logged: `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. Applies without a restart. |

Changes made in the Configuration tab are picked up within about 30 seconds. You don't need to restart the app.

## Panel

### Overview

The overview shows:
- how many users and keys there are
- how many keys are unreviewed or suspicious
- how many keys need attention (flagged and not approved)
- how many login sessions there are
- when the keys were last read

**Read again** re-reads them now. Otherwise they are read every minute, but only when Home Assistant has saved a change.

### One section per user

Owners and administrators come first, and deactivated users come last. The **user card** shows:
- the username, role (Owner, Administrator, User or Read-only), and whether the user is active or local only
- how many keys and sessions the user has
- **Open People**, which opens **Settings → People**

### Key cards

**Title:** the description given when the key was created. Home Assistant calls this the token's name.

**Badge:** the review state, **Unreviewed**, **Approved** or **Suspicious**.

**Stats:**
- **Created**
- **Last used**
- **Last IP:** the address it was last used from
- **Expires**

**Flags** appear below the stats:

| Flag | Meaning |
| --- | --- |
| Deactivated owner | The user is deactivated. The key no longer works, but it is still stored. |
| Expired | Past its expiry date. |
| Never used | Not used since it was created (after one day). |
| Unused | Not used for more than `unused_days`. |
| Vague description | The description is empty, very short, or only generic words like "test token". |
| Duplicate description | The same user has another key with the same description. |
| Admin rights | The user is an owner or administrator, so the key can do anything. Informational only. |

**Buttons:**
- **Approve:** you know who uses the key and why.
- **Suspicious:** you don't recognise it. The card then shows how to revoke it.
- **Reset:** back to Unreviewed.
- **Add note / Edit note:** record who uses the key and what for. Leave it empty to remove the note.
- **How to revoke:** see below.

### Revoking a key

Home Assistant has no API that lets an app or another administrator delete someone else's key. This app stays read-only. To revoke a key:
- **The key's owner** opens **Profile → Security → Long-lived access tokens** and deletes it.
- **An administrator** can deactivate or delete the user in **Settings → People**. A deactivated user's keys stop working at once.

The key disappears from the panel within about a minute, and its review and note are forgotten.

### Login sessions

Switch on **Show login sessions** to list each user's browser and Companion app sign-ins in their user card, with when they signed in and were last used. Sessions have no review state. A user ends a session under **Profile → Security → Refresh tokens**.

### Settings and Phones cards

- **Notifications** switches notifications on or off.
- **Show approved keys** hides or shows keys you have approved. The user card says how many are hidden.
- **Show login sessions** lists sessions in the user cards.
- **Phones** has one switch per Companion app linked to this Home Assistant. Phones are found automatically every 5 minutes, and new phones are switched on.

## How it works

Home Assistant keeps users and tokens in `/config/.storage/auth`. Its API only lists the keys of the user who asks, and an app talks to Home Assistant as the Supervisor, not as you. So the app reads that file directly:
- The configuration folder is mapped **read-only** (`homeassistant_config:ro`). The app can't change it.
- Only these fields are kept:
  - users: name, role, active
  - usernames
  - for each token: id, description, client, created, last used, last IP and expiry
- The token secrets in the file (`token`, `jwt_key`) are thrown away while the file is parsed. They are never stored, logged or shown.
- System users (Supervisor, Home Assistant Content and similar) and their tokens are not listed.
- The file is re-read only when it changes. Home Assistant saves key usage about a minute after the key is used.

**Last used** is updated each time a key is used. On older Home Assistant versions it only showed when the key was created, so every key looked **Never used**.

## Notifications

- A new key is reported once, with its description and user. Keys that already exist when the app first starts are not reported.
- Read failures are reported after 3 failed reads in a row, and again when reading recovers.
- Without any linked phones, the app creates a persistent notification instead.

## Privacy

- **The panel** shows descriptions, usernames and IP addresses, never token values. It is only available to administrators.
- **`/data/state.json`** holds only your reviews and notes (by token id), the ids of keys already seen, and the panel switches. Reviews of deleted keys are removed.
- **The log** shows counts and shortened key ids. Descriptions appear only in notifications.
- **`!secret`** works for the options as in the other apps in this repository. A new secret takes effect after you restart the app.

## Troubleshooting

- **"… not found; is Home Assistant's configuration mapped into the app?":** reinstall or update the app so the read-only `homeassistant_config` mapping is applied, then restart it.
- **A key I just created is missing:** Home Assistant saves new keys within a second. Press **Read again**.
- **Every key says "never" used:** your Home Assistant version doesn't record key usage yet. Update Home Assistant.
