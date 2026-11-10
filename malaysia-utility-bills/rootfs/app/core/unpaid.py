"""When to notify about an unpaid bill, and what to say (pure; identical in the bill-checking apps)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

# A week between reminders; an hour of slack so a weekly check is never just short of it.
REMIND_EVERY = timedelta(days=7) - timedelta(hours=1)

NEW = "new"
REMINDER = "reminder"
PAID = "paid"


def is_due(amount: float | None) -> bool | None:
    """Whether a payment is due (None while the amount is unknown)."""

    return None if amount is None else round(amount, 2) > 0


def unpaid_notice(
    previous: float | None, amount: float | None, notified_at: datetime | None, now: datetime
) -> str | None:
    """Which notice (NEW, REMINDER, PAID) the latest amount due calls for, if any.

    ``previous`` is the amount before this check and ``notified_at`` when the last
    unpaid notice went out (None when none is outstanding).
    """

    if amount is None:
        return None
    if not is_due(amount):
        return PAID if notified_at is not None else None
    if notified_at is None or previous is None or round(previous, 2) != round(amount, 2):
        return NEW
    if now - notified_at >= REMIND_EVERY:
        return REMINDER
    return None


def notice_text(
    kind: str, service: str, account_name: str, amount: float, due_date: date | None = None
) -> tuple[str, str]:
    """Title and message of a notice (the account name and amount only, never the account number)."""

    by = f" by {due_date:%d %b %Y}" if due_date else ""
    if kind == PAID:
        return f"{service} bill paid", f"Nothing is due any more for '{account_name}'."
    if kind == REMINDER:
        return f"{service} bill still unpaid", f"RM {amount:,.2f} is still due for '{account_name}'{by}."
    return f"{service} bill due", f"RM {amount:,.2f} is due for '{account_name}'{by}."


def parse_time(value: object) -> datetime | None:
    """A stored ISO timestamp, or None."""

    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
