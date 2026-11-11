#!/usr/bin/env python3
"""Home Assistant app: API Key Manager - every user's long-lived access tokens, with a review of each one."""

from __future__ import annotations

import copy
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import review
from auth_store import KIND_LONG_LIVED, KIND_SESSION, AuthReader, AuthStoreError, Key, Snapshot, User
from config import Config, ConfigError, apply_log_level, load_config, load_options
from store import Store
from supervisor import (
    Supervisor,
    SupervisorError,
    effective_targets,
    load_supervisor_token,
    merge_discovered,
    merge_s6_environment,
)
from webui import (
    PanelError,
    PanelServer,
    SecretError,
    SecretRefs,
    action,
    badge,
    initial_options,
    link,
    notice,
    row,
    stat,
    toggle,
)

_LOGGER = logging.getLogger("api_key_manager")

VERSION = os.environ.get("AKM_VERSION", "dev")
LOOP_SECONDS = 60
MIN_SLEEP_SECONDS = 1
READ_SECONDS = 60  # Home Assistant saves token usage within a minute
OPTIONS_CHECK_SECONDS = 30
PHONE_CHECK_SECONDS = 300
STORE_SAVE_SECONDS = 60
FAILURES_BEFORE_NOTIFY = 3
MAX_SESSIONS_SHOWN = 20
WEB_DIR = Path(__file__).resolve().parent / "web"
NOTIFICATION_ID = "api_key_manager"
USERS_PATH = "/config/users"
VIEW_DEFAULTS = {"show_approved": True, "show_sessions": False}

STATE_BADGE = {
    review.STATE_UNREVIEWED: ("Unreviewed", "warn"),
    review.STATE_APPROVED: ("Approved", "ok"),
    review.STATE_SUSPICIOUS: ("Suspicious", "error"),
}
STATE_RANK = {review.STATE_SUSPICIOUS: 0, review.STATE_UNREVIEWED: 1, review.STATE_APPROVED: 2}
SERIOUS_FLAGS = frozenset({review.FLAG_INACTIVE_OWNER, review.FLAG_EXPIRED})


def _ago(moment: datetime | None, now: datetime) -> str | None:
    if moment is None:
        return None
    seconds = int((now - moment).total_seconds())
    if seconds < 0:
        return _ahead(moment, now)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


def _ahead(moment: datetime | None, now: datetime) -> str | None:
    if moment is None:
        return None
    seconds = int((moment - now).total_seconds())
    if seconds <= 0:
        return _ago(moment, now)
    days = seconds // 86400
    if days >= 730:  # noqa: PLR2004 - two years
        return f"in {days // 365} years"
    return f"in {days} d" if days else "today"


def _date(moment: datetime | None) -> str | None:
    return moment.astimezone().strftime("%Y-%m-%d") if moment else None


def _short(key_id: str) -> str:
    """Key ids in logs are shortened (they are not secret, but there is no need to print them)."""

    return key_id[:8]


def _flag_text(flag: str, key: Key, config: Config) -> str:
    return {
        review.FLAG_INACTIVE_OWNER: "Its user is deactivated, so it no longer works, but it is still stored.",
        review.FLAG_EXPIRED: f"Expired on {_date(key.expires_at)}.",
        review.FLAG_NEVER_USED: "Never used since it was created.",
        review.FLAG_UNUSED: f"Not used for over {config.unused_days} days.",
        review.FLAG_VAGUE: "The description doesn't say who or what uses it.",
        review.FLAG_DUPLICATE: "Another key of this user has the same description.",
        review.FLAG_ADMIN: "Full administrator rights: its user is an owner or administrator.",
    }[flag]


def _revoke_hint(user: User) -> str:
    return (
        f"Revoke it from {user.name}'s profile (Profile → Security → Long-lived access tokens → Delete). "
        "Administrators can also deactivate or delete the user in Settings → People. "
        "Home Assistant doesn't let an app revoke another user's key."
    )


class App:
    """Reads the auth store, keeps the reviews in /data and serves the panel."""

    def __init__(
        self,
        config: Config,
        supervisor: Supervisor,
        store: Store,
        raw_options: dict[str, object] | None = None,
        reader: AuthReader | None = None,
    ) -> None:
        self.config = config
        self.raw_options = raw_options or {}  # as stored: !secret references, never their values
        self.secrets = SecretRefs()
        self.supervisor = supervisor
        self.store = store
        self.reader = reader or AuthReader()
        self.version = VERSION
        self.snapshot: Snapshot | None = None
        self.flags: dict[str, list[str]] = {}
        self.last_read: datetime | None = None
        self.read_error: str | None = None
        self.read_failures = 0
        self.failure_notified = False
        self.next_read = 0.0
        self.discovered: list[str] = []
        self.next_phone_check = 0.0
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.options_lock = threading.Lock()
        self.lock = threading.Lock()  # store sections changed from the panel thread
        self.pending: tuple[dict[str, object], Config] | None = None

    # -- Options (panel + Configuration tab, applied without a restart) -------------
    def view_config(self) -> Config:
        """Config the panel should show (a saved-but-not-yet-applied change wins)."""

        pending = self.pending
        return pending[1] if pending else self.config

    def apply_config(self, raw: dict[str, object], config: Config) -> None:
        """Switch to new options in place (main thread)."""

        old = self.config
        self.config = config
        self.raw_options = raw
        if (old.unused_days, old.vague_words) != (config.unused_days, config.vague_words):
            self._reflag()
        if old.log_level != config.log_level:
            _LOGGER.info("Log level set to %s", config.log_level)  # before applying: may hide info
            apply_log_level(config.log_level)
        _LOGGER.info("Applied updated options")

    def update_options(self, mutate: Callable[[dict[str, object]], None]) -> None:
        """Change the saved options from the panel; applied on the next loop pass (any thread)."""

        with self.options_lock:
            try:
                base = self.supervisor.fetch_options()
            except SupervisorError as err:
                # Never fall back to the start-up copy: it holds resolved !secret values.
                raise PanelError("Home Assistant is not reachable; try again") from err
            raw = copy.deepcopy(base)
            mutate(raw)
            try:
                config = load_config(self.secrets.resolve(raw))  # type: ignore[arg-type]
            except (ConfigError, SecretError) as err:
                raise PanelError(str(err)) from err
            try:
                self.supervisor.write_options(raw)
            except SupervisorError as err:
                raise PanelError("Home Assistant rejected the change; see the app log") from err
            self.pending = (raw, config)
        self.wake.set()

    def check_options(self) -> None:
        """Pick up edits made in the Configuration tab."""

        try:
            raw = self.supervisor.fetch_options()
        except SupervisorError:
            return
        if raw == self.raw_options:
            return
        try:
            config = load_config(self.secrets.resolve(raw))  # type: ignore[arg-type]
        except (ConfigError, SecretError) as err:
            _LOGGER.error("Ignoring updated options until they are fixed: %s", err)  # noqa: TRY400 - no traceback needed
            self.raw_options = raw
            return
        self.apply_config(raw, config)

    # -- Phones (notification targets for this app) ---------------------------------
    @property
    def phones(self) -> dict[str, object]:
        """Per-phone switches kept in /data (phone name -> notify)."""

        return self.store.section("phones")

    def refresh_phones(self) -> None:
        """Re-list the phones linked to Home Assistant (new phones are notified by default)."""

        found = self.supervisor.discover_mobile_targets()
        if found is None:
            return
        with self.options_lock:
            merged = merge_discovered(self.phones, found)
            changed = merged != self.phones or found != self.discovered
            self.discovered = found
            if merged != self.phones:
                self.store.data["phones"] = merged
                self.store.dirty = True
        if changed:
            targets = effective_targets(self.discovered, self.phones)
            _LOGGER.info("Notifications go to %d phone(s): %s", len(targets), ", ".join(targets) or "-")

    def set_phone(self, name: str, value: bool) -> None:
        """Panel switch for one phone (kept in /data, applies immediately)."""

        with self.options_lock:
            if name not in self.discovered:
                raise PanelError("That phone is no longer linked to Home Assistant; reload the page")
            self.phones[name] = value
            self.store.dirty = True

    def notify(self, title: str, message: str, suffix: str = "") -> None:
        """Notify the switched-on phones (or a persistent notification) when enabled."""

        if self.config.notify_on_new_keys:
            targets = effective_targets(self.discovered, self.phones)
            self.supervisor.notify(targets, title, message, f"{NOTIFICATION_ID}{suffix}")

    # -- Panel view preferences (kept in /data) -----------------------------------------
    def view(self, name: str) -> bool:
        """A panel filter switch (show approved keys, show sessions)."""

        value = self.store.section("view").get(name, VIEW_DEFAULTS[name])
        return value is not False

    # -- Reading the auth store ------------------------------------------------------------
    def _reflag(self) -> None:
        if self.snapshot is not None:
            self.flags = review.all_flags(
                self.snapshot, datetime.now(UTC), self.config.unused_days, self.config.vague_words
            )

    def read(self) -> None:
        """Re-read the auth store when it changed, then refresh flags, reviews and new-key notifications."""

        try:
            snapshot = self.reader.read_if_changed()
        except AuthStoreError as err:
            self.read_failures += 1
            self.read_error = str(err)
            _LOGGER.warning("Reading the auth store failed (%d in a row): %s", self.read_failures, err)
            if self.read_failures >= FAILURES_BEFORE_NOTIFY and not self.failure_notified:
                self.failure_notified = True
                self.notify("API Key Manager can't read the keys", str(err), "_read")
            return
        if self.failure_notified:
            self.notify("API Key Manager recovered", "API keys are being read again.", "_read")
        self.read_failures = 0
        self.failure_notified = False
        self.read_error = None
        self.last_read = datetime.now(UTC)
        if snapshot is not None:
            self._apply_snapshot(snapshot)
        self._reflag()

    def _apply_snapshot(self, snapshot: Snapshot) -> None:
        first_run = "seen" not in self.store.data
        live = {k.key_id for k in snapshot.keys if k.kind == KIND_LONG_LIVED}
        with self.lock:
            self.snapshot = snapshot
            reviews = self.store.section("reviews")
            seen = self.store.section("seen")
            pruned = review.prune(reviews, live) | review.prune(seen, live)
            fresh = review.new_keys(seen, snapshot, first_run=first_run)
            if pruned or fresh or first_run:
                self.store.dirty = True
        lived = len(live)
        _LOGGER.info(
            "Read %d user(s): %d long-lived key(s), %d session(s)",
            len(snapshot.users),
            lived,
            len(snapshot.keys) - lived,
        )
        if fresh:
            users = {u.user_id: u for u in snapshot.users}
            _LOGGER.info("New long-lived key(s): %s", ", ".join(_short(k.key_id) for k in fresh))
            lines = [f"'{k.name or '(no description)'}' for {users[k.user_id].name}" for k in fresh]
            self.notify(
                "New API key" if len(fresh) == 1 else f"{len(fresh)} new API keys",
                f"Created: {'; '.join(lines)}. Review it in API Key Manager.",
                "_new",
            )

    # -- Panel callbacks (HTTP thread) ---------------------------------------------------
    def _key(self, key_id: str) -> Key:
        snapshot = self.snapshot
        for key in snapshot.keys if snapshot else ():
            if key.key_id == key_id and key.kind == KIND_LONG_LIVED:
                return key
        raise PanelError("That key is no longer listed; reload the page")

    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        """Persist and apply one panel switch."""

        kind, _, name = toggle_id.partition(":")
        if kind == "phone" and name:
            self.set_phone(name, value)
            return
        if kind == "view" and name in VIEW_DEFAULTS:
            with self.lock:
                self.store.section("view")[name] = value
                self.store.dirty = True
            return
        if toggle_id == "notify_on_new_keys":
            self.update_options(lambda raw: raw.__setitem__("notify_on_new_keys", value))
            return
        raise PanelError("Unknown setting")

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Handle a panel button."""

        if action_id == "reread":
            self.reader.force()
            self.next_read = 0.0
            self.wake.set()
            return "Reading the keys again..."
        key = self._key(str(args.get("key") or ""))
        label = key.name or "(no description)"
        if action_id == "review":
            state = str(args.get("state") or "")
            if state not in review.STATES:
                raise PanelError("Unknown review state")
            with self.lock:
                review.set_state(self.store.section("reviews"), key.key_id, state)
                self.store.dirty = True
            _LOGGER.info("Key %s marked %s", _short(key.key_id), state)
            return {
                review.STATE_APPROVED: f"Approved '{label}'",
                review.STATE_SUSPICIOUS: f"Marked '{label}' as suspicious",
                review.STATE_UNREVIEWED: f"'{label}' is unreviewed again",
            }[state]
        if action_id == "note":
            with self.lock:
                text = review.set_note(self.store.section("reviews"), key.key_id, str(args.get("text") or ""))
                self.store.dirty = True
            return "Note saved" if text else "Note removed"
        if action_id == "revoke_help":
            users = {u.user_id: u for u in self.snapshot.users} if self.snapshot else {}
            return _revoke_hint(users[key.user_id])
        raise PanelError("Unknown action")

    # -- Panel view model ------------------------------------------------------------------
    def _key_card(self, key: Key, user: User, reviews: dict[str, object], config: Config, now: datetime) -> dict:
        entry = review.review_of(reviews, key.key_id)
        state = entry["state"]
        flags = self.flags.get(key.key_id, [])
        text, tone = STATE_BADGE[state]
        card: dict[str, object] = {
            "title": key.name or "(no description)",
            "icon": "mdi:key-alert" if state == review.STATE_SUSPICIOUS else "mdi:key-variant",
            "subtitle": f"Long-lived access token · {user.name}",
            "badge": badge(text, tone),
            "stats": [
                stat("Created", _ago(key.created_at, now)),
                stat("Last used", _ago(key.last_used_at, now) if review.FLAG_NEVER_USED not in flags else "never"),
                stat("Last IP", key.last_used_ip if review.FLAG_NEVER_USED not in flags else None),
                stat("Expires", _ahead(key.expires_at, now)),
            ],
        }
        rows = []
        if entry["note"]:
            rows.append(row(entry["note"], "Review note", icon="mdi:note-text"))
        if entry["reviewed_at"]:
            reviewed = datetime.fromisoformat(entry["reviewed_at"])
            rows.append(row(f"{text} {_ago(reviewed, now)}", "Review", icon="mdi:shield-check"))
        if rows:
            card["links"] = rows
        messages = [_flag_text(f, key, config) for f in flags]
        if state == review.STATE_SUSPICIOUS:
            messages.append(_revoke_hint(user))
        if messages:
            serious = state == review.STATE_SUSPICIOUS or any(f in SERIOUS_FLAGS for f in flags)
            attention = any(f in review.ATTENTION_FLAGS for f in flags)
            card["notice"] = notice(" ".join(messages), "error" if serious else "warn" if attention else "neutral")
        args = {"key": key.key_id}
        actions = []
        if state != review.STATE_APPROVED:
            actions.append(
                action("review", "Approve", {**args, "state": review.STATE_APPROVED}, icon="mdi:shield-check")
            )
        if state != review.STATE_SUSPICIOUS:
            actions.append(
                action(
                    "review",
                    "Suspicious",
                    {**args, "state": review.STATE_SUSPICIOUS},
                    danger=True,
                    icon="mdi:shield-alert",
                )
            )
        if state != review.STATE_UNREVIEWED:
            actions.append(action("review", "Reset", {**args, "state": review.STATE_UNREVIEWED}, icon="mdi:restore"))
        actions.append(
            action(
                "note",
                "Edit note" if entry["note"] else "Add note",
                args,
                icon="mdi:note-edit",
                prompt=f"Note for '{key.name or 'this key'}' (who uses it and why). Leave empty to remove it.",
                prompt_value=entry["note"],
            )
        )
        actions.append(action("revoke_help", "How to revoke", args, icon="mdi:key-remove"))
        card["actions"] = actions
        return card

    def _user_card(self, user: User, keys: list[Key], sessions: list[Key], hidden: int, now: datetime) -> dict:
        status = "Active" if user.is_active else "Deactivated"
        parts = [f"@{user.username}" if user.username else "", user.role, status]
        if user.local_only:
            parts.append("Local only")
        card: dict[str, object] = {
            "title": user.name,
            "icon": "mdi:crown" if user.is_owner else "mdi:account-off" if not user.is_active else "mdi:account-key",
            "subtitle": " · ".join(p for p in parts if p),
            "badge": badge(status, "ok" if user.is_active else "off"),
            "stats": [stat("API keys", len(keys)), stat("Sessions", len(sessions))],
            "actions": [link("People", USERS_PATH, button="Open People", icon="mdi:open-in-new")],
        }
        if hidden:
            card["notice"] = notice(
                f"{hidden} approved key(s) hidden. Switch on 'Show approved keys' to see them.", "neutral"
            )
        if self.view("show_sessions") and sessions:
            rows = []
            for session in sessions[:MAX_SESSIONS_SHOWN]:
                used = _ago(session.last_used_at, now) or "never"
                where = f" from {session.last_used_ip}" if session.last_used_ip else ""
                rows.append(
                    row(
                        session.client_id or "Login session",
                        f"Signed in {_ago(session.created_at, now) or '-'} · last used {used}{where}",
                        icon="mdi:monitor-cellphone",
                    )
                )
            if len(sessions) > MAX_SESSIONS_SHOWN:
                rows.append(row(f"... and {len(sessions) - MAX_SESSIONS_SHOWN} more", icon="mdi:dots-horizontal"))
            card["links"] = rows
        return card

    def panel_state(self) -> dict[str, object]:
        """The panel view model."""

        config = self.view_config()
        now = datetime.now(UTC)
        snapshot = self.snapshot
        with self.lock:
            reviews = dict(self.store.section("reviews"))
        users = list(snapshot.users) if snapshot else []
        keys = [k for k in snapshot.keys if k.kind == KIND_LONG_LIVED] if snapshot else []
        states = [review.review_of(reviews, k.key_id)["state"] for k in keys]
        attention = sum(
            1
            for k, s in zip(keys, states, strict=True)
            if s != review.STATE_APPROVED and set(self.flags.get(k.key_id, [])) & review.ATTENTION_FLAGS
        )
        unreviewed = states.count(review.STATE_UNREVIEWED)
        suspicious = states.count(review.STATE_SUSPICIOUS)
        if self.read_error:
            chip = badge("Read failing", "error")
        elif snapshot is None:
            chip = badge("Reading...")
        elif suspicious:
            chip = badge(f"{suspicious} suspicious", "error")
        elif unreviewed:
            chip = badge(f"{unreviewed} to review", "warn")
        else:
            chip = badge("All reviewed", "ok")
        overview: dict[str, object] = {
            "title": "Overview",
            "icon": "mdi:key-chain",
            "subtitle": "Long-lived access tokens of every Home Assistant user",
            "badge": chip,
            "stats": [
                stat("Users", len(users)),
                stat("API keys", len(keys)),
                stat("Unreviewed", unreviewed),
                stat("Suspicious", suspicious),
                stat("Need attention", attention),
                stat("Sessions", len(snapshot.keys) - len(keys) if snapshot else None),
                stat("Last read", _ago(self.last_read, now) or "reading..."),
            ],
            "actions": [action("reread", "Read again", icon="mdi:refresh")],
        }
        if self.read_error:
            overview["notice"] = notice(f"Can't read the keys: {self.read_error}", "error")

        sections: list[dict[str, object]] = [{"title": "Summary", "icon": "mdi:view-dashboard", "cards": [overview]}]
        show_approved = self.view("show_approved")
        for user in users:
            own = snapshot.keys_of(user.user_id, KIND_LONG_LIVED) if snapshot else []
            sessions = snapshot.keys_of(user.user_id, KIND_SESSION) if snapshot else []
            rank = {k.key_id: STATE_RANK[review.review_of(reviews, k.key_id)["state"]] for k in own}
            visible = [k for k in own if show_approved or rank[k.key_id] != STATE_RANK[review.STATE_APPROVED]]
            visible.sort(key=lambda k: rank[k.key_id])  # stable: newest first within a state
            cards = [self._user_card(user, own, sessions, len(own) - len(visible), now)]
            cards += [self._key_card(k, user, reviews, config, now) for k in visible]
            sections.append({"title": user.name, "icon": "mdi:account", "cards": cards})

        settings = {
            "title": "Settings",
            "icon": "mdi:cog",
            "subtitle": "Edit the unused threshold and extra vague words in the Configuration tab",
            "stats": [stat("Unused after", config.unused_days, "days")],
            "toggles": [
                toggle(
                    "notify_on_new_keys",
                    "Notifications",
                    config.notify_on_new_keys,
                    "Notify the phones below when a new API key is created or the keys can't be read",
                    icon="mdi:bell",
                ),
                toggle("view:show_approved", "Show approved keys", show_approved, icon="mdi:eye"),
                toggle(
                    "view:show_sessions",
                    "Show login sessions",
                    self.view("show_sessions"),
                    "Browser and Companion app sign-ins, listed in each user's card",
                    icon="mdi:monitor-cellphone",
                ),
            ],
        }
        phones = {
            "title": "Phones",
            "icon": "mdi:cellphone-message",
            "subtitle": "Companion apps linked to this Home Assistant (found automatically)",
            "toggles": [
                toggle(
                    f"phone:{name}",
                    name.removeprefix("mobile_app_").replace("_", " "),
                    self.phones.get(name, True) is not False,
                    name,
                    disabled=not config.notify_on_new_keys,
                    icon="mdi:cellphone",
                )
                for name in sorted(self.discovered)
            ],
            "empty": "No mobile apps found. Without phones a persistent notification is used.",
        }
        sections.append({"title": "Settings", "icon": "mdi:cog", "cards": [settings, phones]})
        return {
            "title": "API Key Manager",
            "subtitle": f"Version {self.version} · {len(keys)} key(s) across {len(users)} user(s)",
            "sections": sections,
        }

    # -- Loop ----------------------------------------------------------------------------------
    def _tick(self) -> None:
        with self.options_lock:
            pending, self.pending = self.pending, None
        if pending is not None:
            self.apply_config(*pending)
        now = time.monotonic()
        if now >= self.next_options_check:
            self.next_options_check = now + OPTIONS_CHECK_SECONDS
            self.check_options()
        if now >= self.next_phone_check:
            self.next_phone_check = now + PHONE_CHECK_SECONDS
            self.refresh_phones()
        if now >= self.next_read:
            self.next_read = now + READ_SECONDS
            self.read()

    def run(self) -> None:
        """Serve the panel and run the loop until stopped."""

        server = PanelServer(WEB_DIR, self.panel_state, self.panel_toggle, self.panel_action)
        server.start()
        last_save = time.monotonic()
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                self._tick()
                if time.monotonic() - last_save >= STORE_SAVE_SECONDS or self.store.dirty:
                    with self.lock:
                        self.store.save()
                    last_save = time.monotonic()
                deadline = min(self.next_read, self.next_options_check, self.next_phone_check)
                self.wake.wait(min(max(deadline - time.monotonic(), MIN_SLEEP_SECONDS), LOOP_SECONDS))
        finally:
            server.stop()
            self.store.save()
            _LOGGER.info("Stopped")


def main() -> None:
    """Entry point."""

    merge_s6_environment()  # TZ first, so every log line uses local time
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    _LOGGER.info("Starting API Key Manager %s", VERSION)
    token = load_supervisor_token()
    if not token:
        _LOGGER.error("No supervisor token; cannot reach Home Assistant")
        raise SystemExit(1)
    supervisor = Supervisor(token)

    secrets = SecretRefs()
    raw_options = initial_options(load_options, supervisor.fetch_options, secrets)
    try:
        config = load_config(secrets.resolve(raw_options))  # type: ignore[arg-type]
    except (ConfigError, SecretError):
        _LOGGER.exception("Invalid configuration")
        raise SystemExit(1) from None
    if config.log_level != "info":
        _LOGGER.info("Log level set to %s", config.log_level)
    apply_log_level(config.log_level)

    store = Store()
    store.load()
    app = App(config, supervisor, store, raw_options)
    app.secrets = secrets

    def _stop(signum: int, _frame: FrameType | None) -> None:
        _LOGGER.info("Received signal %d; shutting down", signum)
        app.stopping.set()
        app.wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    app.run()


if __name__ == "__main__":
    main()
