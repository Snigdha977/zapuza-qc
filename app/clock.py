"""Injectable clock so hold timers and gauge expiry can be tested deterministically."""
from datetime import datetime, timezone

_override = None


def now() -> datetime:
    return _override or datetime.now(timezone.utc)


def set_now(dt: datetime) -> None:
    global _override
    _override = dt


def reset() -> None:
    global _override
    _override = None


def iso() -> str:
    return now().isoformat()


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)
