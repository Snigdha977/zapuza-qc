"""Pure business rules: tolerance evaluation, field validation, hold-time formatting.

All measurement maths uses Decimal so boundary values (e.g. 25.05 against a
25.00 +0.05 limit) are exact; binary floats would make that comparison unreliable.
"""
import math
from decimal import Decimal, InvalidOperation

KINDS = ("range", "max", "min", "visual")


def parse_decimal(v) -> Decimal:
    if isinstance(v, bool):
        raise ValueError("A numeric value is required")
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Value must be a number")
    if not d.is_finite():
        raise ValueError("Value must be a finite number")
    return d


def parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("yes", "true", "pass", "ok", "1"):
        return True
    if s in ("no", "false", "fail", "0"):
        return False
    raise ValueError("Visual check must be Yes or No")


def evaluate(field: dict, value):
    """Return (normalised_raw_value, passed) for one inspection field."""
    kind = field["kind"]
    if kind == "visual":
        b = parse_bool(value)
        return ("yes" if b else "no", b)
    d = parse_decimal(value)
    if kind == "range":
        nominal = Decimal(field["nominal"])
        low = nominal + Decimal(field["lower_tol"])
        high = nominal + Decimal(field["upper_tol"])
        ok = low <= d <= high
    elif kind == "max":
        ok = d <= Decimal(field["limit_value"])
    elif kind == "min":
        ok = d >= Decimal(field["limit_value"])
    else:
        raise ValueError("Unknown field kind")
    return (format(d, "f"), ok)


def describe_limits(field: dict) -> str:
    kind, unit = field["kind"], field.get("unit") or ""
    if kind == "range":
        nominal = Decimal(field["nominal"])
        low = nominal + Decimal(field["lower_tol"])
        high = nominal + Decimal(field["upper_tol"])
        return f"{format(low, 'f')} to {format(high, 'f')} {unit}".strip()
    if kind == "max":
        return f"max {field['limit_value']} {unit}".strip()
    if kind == "min":
        return f"min {field['limit_value']} {unit}".strip()
    return "Yes / No"


def validate_field_def(f: dict) -> dict:
    """Validate and normalise a template field definition (raises ValueError)."""
    label = (f.get("label") or "").strip()
    if not label:
        raise ValueError("Every field needs a label")
    kind = f.get("kind")
    if kind not in KINDS:
        raise ValueError(f"Field '{label}': kind must be one of {', '.join(KINDS)}")
    out = {"label": label, "kind": kind, "unit": (f.get("unit") or "").strip(),
           "ctq": 1 if f.get("ctq") else 0,
           "nominal": None, "upper_tol": None, "lower_tol": None, "limit_value": None}
    try:
        if kind == "range":
            nominal = parse_decimal(f.get("nominal"))
            upper = parse_decimal(f.get("upper_tol"))
            lower = parse_decimal(f.get("lower_tol"))
            if lower > 0 or upper < 0:
                raise ValueError("lower_tol must be <= 0 and upper_tol must be >= 0")
            out.update(nominal=format(nominal, "f"), upper_tol=format(upper, "f"),
                       lower_tol=format(lower, "f"))
        elif kind in ("max", "min"):
            out["limit_value"] = format(parse_decimal(f.get("limit_value")), "f")
    except ValueError as e:
        raise ValueError(f"Field '{label}': {e}")
    return out


def format_remaining(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} hr {m} min"
    if h:
        return f"{h} hr"
    return f"{m} min"
