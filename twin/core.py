"""Kleene logic, ownership state, events, date helpers. Pure functions, no I/O."""
from dataclasses import dataclass, replace, field
from datetime import date, timedelta
from enum import IntEnum
from dateutil.relativedelta import relativedelta


class K(IntEnum):
    """Strong Kleene logic: AND = min, OR = max, NOT = 2 - x."""
    F = 0
    U = 1
    T = 2


def k_and(*xs): return K(min(xs, default=K.T))
def k_or(*xs): return K(max(xs, default=K.F))
def k_not(x): return K(2 - x)
def k(b): return K.T if b else K.F


@dataclass(frozen=True)
class State:
    """Company-side facts after the events so far (cap table + structure + clock)."""
    as_of: date = date(2027, 1, 1)
    buyer_pct: float = 0.0              # largest single new holder (any one person/group)
    retained_pct: dict = field(default_factory=lambda: {"sponsors": 100.0, "principal_owners": 100.0, "original_owners": 100.0})
    largest_other_pct: float = 0.0      # largest holder other than the retained holders
    listed: bool = False
    ipo_proceeds: float = 0.0
    management_changed: bool = False
    mechanism: str = ""                 # merger / consolidation / reorganization / asset_sale / share_sale / ipo
    holding_position: str = ""          # above_top_parent / between_parent_and_borrower
    buyer_is_competitor: bool = False
    consented: bool = False
    prev: "State | None" = None         # lineage (CDM-style): reference to the state before this event


def apply(s: State, ev: dict) -> State:
    """One event -> new state. Atomic: either the whole event applies or the call raises."""
    e = ev["event"]
    d = date.fromisoformat(ev["date"]) if "date" in ev else s.as_of
    if e == "date_advance":
        return replace(s, as_of=date.fromisoformat(ev["to"]), prev=s)
    if e == "share_sale":
        ret = dict(s.retained_pct)
        buyer = ev.get("buyer_pct", ev.get("largest_other_pct_after", 0))
        if "sponsor_pct_after" in ev:
            ret["sponsors"] = ev["sponsor_pct_after"]
        else:
            ret = {h: min(p, 100 - buyer) for h, p in ret.items()}
        return replace(s, as_of=d, buyer_pct=buyer, retained_pct=ret,
                       largest_other_pct=ev.get("largest_other_pct_after", buyer),
                       management_changed=ev.get("management_changes", True),
                       mechanism="share_sale", buyer_is_competitor=ev.get("buyer_is_competitor", False), prev=s)
    if e == "ipo":
        ret = dict(s.retained_pct)
        if "sponsor_pct_after" in ev:
            ret["sponsors"] = ev["sponsor_pct_after"]
        if "float_pct" in ev:
            ret = {h: min(p, 100 - ev["float_pct"]) for h, p in ret.items()}
        proceeds = ev.get("gross_proceeds_usd", 0)
        return replace(s, as_of=d, listed=True, ipo_proceeds=proceeds,
                       largest_other_pct=ev.get("largest_other_pct_after", 0),
                       management_changed=ev.get("management_changes", False), mechanism="ipo", retained_pct=ret, prev=s)
    if e == "holding_insertion":
        return replace(s, as_of=d, mechanism=ev.get("mechanism", "reorganization"),
                       holding_position=ev.get("position", "above_top_parent"), prev=s)
    raise ValueError(f"unknown event {e}")


def add_period(d: date, n: int, unit: str, sign: int = 1) -> date | None:
    """Calendar arithmetic. business_days: Mon-Fri, no holidays (stated assumption). None = cannot compute."""
    if unit == "days":
        return d + timedelta(days=sign * n)
    if unit == "months":
        return d + relativedelta(months=sign * n)
    if unit == "years":
        return d + relativedelta(years=sign * n)
    if unit == "business_days":
        step, left = timedelta(days=sign), n
        while left:
            d += step
            if d.weekday() < 5:
                left -= 1
        return d
    return None  # missing_unit, redacted
