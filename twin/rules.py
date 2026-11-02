"""Templates as code. Each rule returns Reaction(status, kind, holder, due, reason, atoms). No model calls."""
from dataclasses import dataclass
from datetime import date
from .core import K, k, k_and, k_or, k_not, State, add_period

STATUS = {K.T: "TRIGGERED", K.F: "NOT_TRIGGERED", K.U: "NEEDS_LAWYER"}


@dataclass
class Reaction:
    status: str
    kind: str = ""
    holder: str = ""
    due: date | None = None
    reason: str = ""
    atoms: tuple = ()


def cmp(x, op, t): return x > t if op == "gt" else x >= t


# ---------------- change of control ----------------
def coc_trigger(a: dict, s: State) -> tuple[K, str]:
    """Is there a change of control of US under this clause? Returns (truth, reason)."""
    if a.get("whose") == "them":
        return K.F, "clause concerns the counterparty's change of control"
    trig, why = [], []
    for t in a.get("triggers", []):
        if t == "ownership_threshold":
            v = k(cmp(s.buyer_pct, a.get("threshold_op", "gte"), a["threshold_pct"]))
            why.append(f"buyer {s.buyer_pct}% vs {a.get('threshold_op','gte')} {a['threshold_pct']}%")
        elif t == "retained_stake":
            q = a.get("qualifying_ipo")
            qipo = bool(q) and s.listed and (q["test"] != "proceeds" or s.ipo_proceeds >= q["threshold"])
            if qipo:   # Catala-style exception: after a Qualifying IPO the post-IPO test replaces it
                held = s.retained_pct.get(a["retained_holder"], 100)
                v = k(s.largest_other_pct > a["post_ipo_threshold_pct"] and
                      (not a.get("post_ipo_relative_to_retained") or s.largest_other_pct >= held))
                why.append(f"post-QIPO: other holder {s.largest_other_pct}% vs > {a['post_ipo_threshold_pct']}% and >= {a['retained_holder']} {held}%")
            else:
                held = s.retained_pct.get(a["retained_holder"], 100)
                v = k(held < a["retained_pct"])
                why.append(f"{a['retained_holder']} keep {held}% vs required {a['retained_pct']}%")
        elif t == "management_change":
            v = k(s.management_changed and s.mechanism in ("share_sale", "ipo", "merger"))
            why.append(f"management changed: {s.management_changed}")
        elif t == "transaction_type":
            v = k(s.mechanism in a.get("transaction_types", []))
            why.append(f"mechanism {s.mechanism or 'none'} vs {a.get('transaction_types')}")
        elif t == "open_textured":
            if a.get("open_texture") == "judgement":
                v = K.U if s.mechanism else K.F
                why.append("counterparty judgement standard")
            else:   # magnitude word: a majority change is significant/material on any reading
                v = K.T if s.buyer_pct > 50 else (K.U if s.buyer_pct > 0 else K.F)
                why.append(f"open-textured magnitude, buyer {s.buyer_pct}%")
        elif t == "parent_direct_ownership":
            v = K.U if s.holding_position == "between_parent_and_borrower" else K.F
            why.append(f"holding position {s.holding_position or 'none'}")
        elif t in ("cross_reference", "undefined"):
            v = K.U if s.mechanism else K.F
            why.append("depends on text not in the file" if t == "cross_reference" else "definition not located")
        else:
            v = K.U
            why.append(f"unknown trigger kind {t}")
        trig.append(v)
    out = k_or(*trig)
    co = a.get("carve_outs", [])
    if "same_management" in co and not s.management_changed:
        out, why = K.F, why + ["carve-out: same management"]
    if "consented" in co and s.consented:
        out, why = K.F, why + ["carve-out: consented"]
    return out, "; ".join(why)


def coc_reactions(a: dict, s: State) -> list[Reaction]:
    t, why = coc_trigger(a, s)
    effects = a.get("effects") or [{"effect": a.get("effect"), "effect_holder": a.get("effect_holder", ""), "notice": a.get("notice")}]
    out = []
    for e in effects:
        st = t
        if t == K.T and e["effect"] == "elsewhere":
            st, why2 = K.U, why + "; effect defined outside the file"
        else:
            why2 = why
        due = None
        if st == K.T and e["effect"] == "notice" and e.get("notice"):
            due = add_period(s.as_of, *e["notice"])
        out.append(Reaction(STATUS[st], e["effect"] if st != K.F else "", e.get("effect_holder", ""), due, why2, ("coc",)))
    return out


# ---------------- assignment ----------------
def assign_reactions(a: dict, s: State) -> list[Reaction]:
    if not s.mechanism or s.mechanism == "ipo":
        return [Reaction("NOT_TRIGGERED", reason="no transfer by us")]
    if s.mechanism == "share_sale":
        if a.get("coc_not_assignment"):
            return [Reaction("NOT_TRIGGERED", reason="change of control expressly not an assignment", atoms=("coc_not_assignment",))]
        if "change_of_control" in a.get("deems", []):
            return [Reaction("TRIGGERED", "consent", "them", reason="change of control deemed assignment")]
        if a.get("merger_exception") and a.get("unless_competitor") and s.buyer_is_competitor:
            return [Reaction("NEEDS_LAWYER", reason="competitor exception may apply, but a share sale may not be an assignment")]
        return [Reaction("NOT_TRIGGERED", reason="a share sale is not an assignment by the company")]
    hit = s.mechanism in a.get("deems", [])
    if hit and not (a.get("merger_exception") and not (a.get("unless_competitor") and s.buyer_is_competitor)):
        return [Reaction("TRIGGERED", "consent", "them", reason=f"{s.mechanism} deemed assignment", atoms=("deems",))]
    return [Reaction("NOT_TRIGGERED", reason=f"{s.mechanism} not deemed an assignment")]


# ---------------- term and renewal ----------------
def expiry(a: dict) -> date | None:
    if "end_date" in a:
        return date.fromisoformat(a["end_date"])
    if a.get("initial_term") == "redacted" or "start" not in a:
        return None
    n, u = a["initial_term"]
    return add_period(add_period(date.fromisoformat(a["start"]), n, u), 1, "days", -1)


def term_reactions(a: dict, s: State) -> list[Reaction]:
    mode = a.get("mode")
    if mode == "conditional":
        return [Reaction("NEEDS_LAWYER", reason="renewal depends on facts outside the text")]
    exp = expiry(a)
    if exp is None:
        return [Reaction("NEEDS_LAWYER", reason="term start or length not determinable from the text")]
    if mode == "mutual":
        return [Reaction("TRIGGERED", "expiry", "", exp, "renews only by mutual agreement")]
    n, u = a.get("notice", (None, None))
    if mode == "evergreen":
        if s.as_of <= exp:
            return [Reaction("TRIGGERED", "expiry", "", exp, "initial term running")]
        due = add_period(s.as_of, n, u)
        return [Reaction("NEEDS_LAWYER" if due is None else "TRIGGERED", "earliest_exit", "either", due, "evergreen: notice now ends it after the notice period")]
    deadline = add_period(exp, n, u, -1) if n is not None else None
    if deadline is None:
        return [Reaction("NEEDS_LAWYER", reason=f"notice period unit not determinable ({u})")]
    kind = "option_deadline" if mode == "option" else "deadline"
    if s.as_of <= deadline:
        return [Reaction("TRIGGERED", kind, a.get("option_holder", "both") if mode == "option" else "both", deadline, f"expiry {exp}")]
    if s.as_of <= exp:
        return [Reaction("TRIGGERED", kind + "_lapsed" if mode != "option" else "option_lapsed", "", deadline, f"expiry {exp}")]
    if mode == "option":
        return [Reaction("TRIGGERED", "expired", "", exp, "option not exercised (assumed)")]
    return [Reaction("TRIGGERED", "renewed", "", None, f"past expiry {exp}")]


TEMPLATES = {"coc": coc_reactions, "assign": assign_reactions, "term": term_reactions}
