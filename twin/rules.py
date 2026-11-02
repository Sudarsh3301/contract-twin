"""Templates as code. Each rule returns Reaction(status, kind, holder, due, reason, atoms, cause, trace). No model calls.

Every reaction carries a trace: the steps the rule evaluated, in order. A step is a fact of the event, an atom of the
clause, one trigger test (limb), an exception checked, an effect, a reading tried for an unknown atom (fork) or the
outcome. The reason string is the labels of the tests that decided the outcome, joined, so it is read from the trace."""
from dataclasses import dataclass, field, replace
from datetime import date
from .core import K, k, k_and, k_or, k_not, State, add_period

STATUS = {K.T: "TRIGGERED", K.F: "NOT_TRIGGERED", K.U: "NEEDS_LAWYER"}
VALUE = {v: k_ for k_, v in STATUS.items()}


@dataclass(frozen=True)
class Step:
    """One evaluated node. kind: fact, atom, limb, exception, effect, fork, outcome. Ids name what the step is about
    (fact:buyer_pct, atom:threshold_pct, limb:ownership_threshold), so the same step lines up across scenarios."""
    id: str
    kind: str
    label: str
    inputs: tuple = ()          # (name, value) pairs
    value: K = K.T
    atom_refs: tuple = ()
    parents: tuple = ()


@dataclass
class Reaction:
    status: str
    kind: str = ""
    holder: str = ""
    due: date | None = None
    reason: str = ""
    atoms: tuple = ()
    cause: str = ""     # NEEDS_LAWYER only: why the outcome is open; several causes joined by "," in CAUSES order
    trace: tuple = field(default=(), compare=False, repr=False)


_RECORD = [True]       # False while readings are enumerated: only the reason is kept, steps are not built


class quiet:
    """Within this block rules build no steps (the reason string is still built). Used where many readings are tried
    and only a few are kept; the kept ones are evaluated again with steps."""

    def __enter__(self):
        self.was, _RECORD[0] = _RECORD[0], False

    def __exit__(self, *exc):
        _RECORD[0] = self.was


class Trace:
    """Records steps while a rule evaluates. `why` holds the labels that make up the reason string."""

    def __init__(self):
        self.steps, self.why = {}, []

    def copy(self) -> "Trace":
        t = Trace()
        t.steps, t.why = dict(self.steps), list(self.why)
        return t

    def add(self, id, kind, label, value=K.T, parents=(), atoms=(), inputs=None, say=False):
        if not _RECORD[0]:
            if say:
                self.why.append(label)
            return id
        if id not in self.steps:
            self.steps[id] = Step(id, kind, label, tuple(sorted((inputs or {}).items())), value, tuple(atoms), tuple(parents))
        if say:
            self.why.append(label)
        return id

    def fact(self, name, label, value=K.T, x=None):
        return self.add("fact:" + name, "fact", label, value, inputs={"value": x})

    def atom(self, a: dict, name, ref=None):
        if not _RECORD[0]:
            return "atom:" + (ref or name)
        v = a.get(name)
        shown = "not in the clause" if v is None else ", ".join(map(str, v)) or "none" if isinstance(v, list) else v
        return self.add("atom:" + (ref or name), "atom", f"{name} = {shown}", K.T, atoms=(ref or name,), inputs={"value": v})

    def limb(self, id, label, value, parents, say=True):
        return self.add("limb:" + id, "limb", label, value, parents, say=say)

    def exception(self, id, label, value, parents, say=False):
        return self.add("exc:" + id, "exception", label, value, parents, say=say)

    def reason(self) -> str:
        return "; ".join(self.why)

    def done(self, status, kind="", holder="", due=None, atoms=(), cause="", parents=None) -> Reaction:
        """Close the trace with the outcome step and return the reaction it decides."""
        if not _RECORD[0]:
            return Reaction(status, kind, holder, due, self.reason(), atoms, cause)
        if parents is None:
            last = [s.id for s in self.steps.values() if s.kind == "effect"] or \
                   [s.id for s in self.steps.values() if s.kind in ("limb", "exception", "fork")]
            parents = last
        what = "no reaction" if status == "NOT_TRIGGERED" else ("open: " + (cause or "unknown")) if status == "NEEDS_LAWYER" else \
            kind.replace("_", " ") + (f" ({holder})" if holder else "") + (f" by {due}" if due else "")
        out = Step("outcome", "outcome", what, (("cause", cause), ("due", str(due) if due else ""), ("holder", holder),
                                                 ("kind", kind), ("status", status)), VALUE[status], (), tuple(parents))
        return Reaction(status, kind, holder, due, self.reason(), atoms, cause, tuple(self.steps.values()) + (out,))


def traced(status, reason, kind="", holder="", due=None, atoms=(), cause="", tr=None) -> Reaction:
    """A reaction decided by one test whose label is the reason."""
    tr = tr or Trace()
    tr.limb("decided", reason, VALUE[status],
            [s for s in tr.steps if s.startswith(("limb:", "exc:", "fork:"))] or list(tr.steps))
    return tr.done(status, kind, holder, due, atoms, cause)


# Why an outcome is open, most important first. legal_judgement: an open-textured standard or a rule that states two
# readings, the only cause that needs counsel. missing_document: the answer is in another instrument, a definition not
# located or a redacted term. missing_fact: a business fact, not the text. check_extraction: the compiler is unsure of an atom.
CAUSES = ["legal_judgement", "missing_document", "missing_fact", "check_extraction"]


def join_causes(*cs) -> str:
    got = {c for x in cs for c in x.split(",") if c}
    return ",".join(c for c in CAUSES if c in got)


def cmp(x, op, t): return x > t if op == "gt" else x >= t


# ---------------- change of control ----------------
def coc_trigger(a: dict, s: State) -> tuple[K, str]:
    """Is there a change of control of US under this clause? Returns (truth, reason)."""
    out, why, _ = _coc_trigger(a, s)
    return out, why


def aggregate_change(s: State) -> float:
    """Share of the company changing hands in total: the larger of the biggest buyer and what the original holders gave up."""
    return max(s.buyer_pct, 100 - min(s.retained_pct.values(), default=100))


def _coc_trigger(a: dict, s: State, tr: Trace | None = None) -> tuple[K, str, dict]:
    """(truth, reason, {trigger kind: cause} for each limb that is unknown when the whole trigger is unknown).
    Steps go into `tr` when given."""
    tr = tr if tr is not None else Trace()
    w = tr.atom(a, "whose")
    if a.get("whose") == "them":
        tr.limb("whose", "clause concerns the counterparty's change of control", K.F, [w])
        return K.F, tr.reason(), {}
    trig, open_ = [], {}
    ta = tr.atom(a, "triggers")
    mech = lambda: tr.fact("mechanism", f"transaction: {(s.mechanism or 'none').replace('_', ' ')}", k(s.mechanism), s.mechanism)
    buyer = lambda: tr.fact("buyer_pct", f"buyer gets {s.buyer_pct}%", K.T, s.buyer_pct)
    for t in a.get("triggers", []):
        if t == "ownership_threshold":
            # "any person acquires X%" (single acquirer) and "a change in ownership of X%" (aggregate) differ when shares
            # spread to many holders (an IPO); a new holding company acquires all direct shares while the ultimate owners
            # stay the same. When the clause does not say which it means, both readings are tried.
            op, thr, measure = a.get("threshold_op", "gte"), a["threshold_pct"], a.get("threshold_measure")
            agg = aggregate_change(s)
            single, whole = cmp(s.buyer_pct, op, thr), cmp(agg, op, thr)
            v = k(single) if measure == "single" else k(whole) if measure == "aggregate" else k(single) if single == whole else K.U
            if s.holding_position and measure != "aggregate" and not single:
                v = K.U     # the new parent acquires 100% of the direct shares: a reading question
            ps = [ta, buyer(), tr.fact("aggregate_change", f"ownership changing hands in total: {agg}%", K.T, agg),
                  tr.atom(a, "threshold_pct"), tr.atom(a, "threshold_op")]
            label = f"buyer {s.buyer_pct}% vs {op} {thr}%" + (
                "" if v != K.U else " (single acquirer and total change in ownership read differently)" if not s.holding_position
                else " (does a new parent company count as an acquirer?)")
            if v == K.U:
                open_[t] = "legal_judgement"
        elif t == "retained_stake":
            q = a.get("qualifying_ipo")
            qipo = bool(q) and s.listed and (q["test"] != "proceeds" or s.ipo_proceeds >= q["threshold"])
            held = s.retained_pct.get(a["retained_holder"], 100)
            ps = [ta, tr.atom(a, "retained_holder"),
                  tr.fact("retained", f"{a['retained_holder']} keep {held}%", K.T, held)]
            qa = tr.atom(a, "qualifying_ipo")
            if q:
                qe = tr.exception("qualifying_ipo", "a qualifying IPO happened" if qipo else "no qualifying IPO", k(qipo),
                                  [qa, tr.fact("listed", "company listed", k(s.listed), s.listed),
                                   tr.fact("ipo_proceeds", f"IPO proceeds {s.ipo_proceeds:,.0f}", K.T, s.ipo_proceeds)])
                ps.append(qe)
            if qipo:   # Catala-style exception: after a Qualifying IPO the post-IPO test replaces it
                v = k(s.largest_other_pct > a["post_ipo_threshold_pct"] and
                      (not a.get("post_ipo_relative_to_retained") or s.largest_other_pct >= held))
                ps += [tr.fact("largest_other_pct", f"largest other holder {s.largest_other_pct}%", K.T, s.largest_other_pct),
                       tr.atom(a, "post_ipo_threshold_pct"), tr.atom(a, "post_ipo_relative_to_retained")]
                label = f"post-QIPO: other holder {s.largest_other_pct}% vs > {a['post_ipo_threshold_pct']}% and >= {a['retained_holder']} {held}%"
            else:
                v = k(held < a["retained_pct"])
                ps.append(tr.atom(a, "retained_pct"))
                label = f"{a['retained_holder']} keep {held}% vs required {a['retained_pct']}%"
        elif t == "competitor_stake":
            v = k(s.buyer_is_competitor and s.buyer_pct >= a.get("competitor_threshold_pct", 5))
            ps = [ta, tr.fact("buyer_is_competitor", "buyer is a competitor", k(s.buyer_is_competitor), s.buyer_is_competitor),
                  buyer(), tr.atom(a, "competitor_threshold_pct")]
            label = f"competitor stake {s.buyer_pct if s.buyer_is_competitor else 0}% vs >= {a.get('competitor_threshold_pct', 5)}%"
        elif t == "management_change":
            v = k(s.management_changed and s.mechanism in ("share_sale", "ipo", "merger"))
            ps = [ta, tr.fact("management_changed", "management changes", k(s.management_changed), s.management_changed), mech()]
            label = f"management changed: {s.management_changed}"
        elif t == "transaction_type":
            types = a.get("transaction_types", [])
            v = k(s.mechanism in types)
            if s.mechanism == "share_exchange" and "reorganization" in types and not v:
                v = K.U     # is a share-for-share exchange with a new parent a "reorganization"? the same reading question as in assignment
                open_[t] = "legal_judgement"
            ps = [ta, mech(), tr.atom(a, "transaction_types")]
            label = f"mechanism {s.mechanism or 'none'} vs {a.get('transaction_types')}"
        elif t == "open_textured":
            ps = [ta, tr.atom(a, "open_texture")]
            if a.get("open_texture") == "judgement":
                v = K.U if s.mechanism else K.F
                ps.append(mech())
                label = "counterparty judgement standard"
            else:   # magnitude word: a majority change is significant/material on any reading
                size = max(s.buyer_pct, aggregate_change(s))
                v = K.T if size > 50 else (K.U if size > 0 or s.holding_position else K.F)
                ps.append(buyer())
                label = f"open-textured magnitude, buyer {s.buyer_pct}%" + (
                    f", {size}% changing hands in total" if size != s.buyer_pct else "") + (
                    ", new parent company" if s.holding_position and size == 0 else "")
            open_[t] = "legal_judgement"
        elif t == "parent_direct_ownership":
            v = K.U if s.holding_position == "between_parent_and_borrower" else K.F
            ps = [ta, tr.fact("holding_position", f"new holding company: {(s.holding_position or 'none').replace('_', ' ')}",
                              k(s.holding_position), s.holding_position)]
            label = f"holding position {s.holding_position or 'none'}"
            open_[t] = "legal_judgement"
        elif t in ("cross_reference", "undefined"):
            v = K.U if s.mechanism else K.F
            ps = [ta, mech()]
            label = "depends on text not in the file" if t == "cross_reference" else "definition not located"
            open_[t] = "missing_document"
        else:
            v, ps, label = K.U, [ta], f"unknown trigger kind {t}"
            open_[t] = "check_extraction"
        tr.limb(t, label, v, [w] + ps)
        if v != K.U:
            open_.pop(t, None)
        trig.append(v)
    out = k_or(*trig)
    co = a.get("carve_outs", [])
    if co:
        ca = tr.atom(a, "carve_outs")
    if "same_management" in co:
        hit = not s.management_changed
        tr.exception("same_management", "carve-out: same management" if hit else "carve-out same management: management changes",
                     k(hit), [ca, tr.fact("management_changed", "management changes", k(s.management_changed), s.management_changed)],
                     say=hit)
        if hit:
            out = K.F
    if "consented" in co:
        tr.exception("consented", "carve-out: consented" if s.consented else "carve-out consented: no prior consent",
                     k(s.consented), [ca, tr.fact("consented", "counterparty consented in advance", k(s.consented), s.consented)],
                     say=s.consented)
        if s.consented:
            out = K.F
    return out, tr.reason(), open_ if out == K.U else {}


def coc_reactions(a: dict, s: State) -> list[Reaction]:
    tr = Trace()
    t, why, open_ = _coc_trigger(a, s, tr)
    return coc_effects(a, s, t, why, join_causes(*open_.values()), tr)


def coc_effects(a: dict, s: State, t: K, why: str, cause: str = "", tr: Trace | None = None) -> list[Reaction]:
    """The clause's effects once the truth of the trigger is settled. `tr` holds the trigger's steps (a fresh trace
    whose only step is the trigger's reason when not given)."""
    if tr is None:
        tr = Trace()
        tr.limb("trigger", why, t, [], say=bool(why))
    effects = a.get("effects") or [{"effect": a.get("effect"), "effect_holder": a.get("effect_holder", ""), "notice": a.get("notice")}]
    ea = tr.add("atom:effects", "atom", "effects = " + ", ".join(f"{e.get('effect')} ({e.get('effect_holder', '?')})" for e in effects),
                atoms=("effects",), inputs={"value": effects})
    decided = [s_.id for s_ in tr.steps.values() if s_.kind in ("limb", "exception", "fork")]
    out = []
    for i, e in enumerate(effects):
        b = tr.copy()
        st, c, ps = t, cause, decided + [ea]
        if t == K.T and e.get("unless_consented") and s.consented:
            st = K.F
            ps.append(b.exception(f"consented_{i}", "counterparty consented in advance", K.T,
                                  [ea, b.fact("consented", "counterparty consented in advance", K.T, True)], say=True))
        elif t == K.T and e["effect"] == "elsewhere":
            st, c = K.U, "missing_document"
            b.why.append("effect defined outside the file")
        due = None
        if st == K.T and e["effect"] == "notice" and e.get("notice"):
            due = add_period(s.as_of, *e["notice"])
            ps.append(b.fact("as_of", f"as of {s.as_of}", K.T, str(s.as_of)))
        holder = e.get("effect_holder", "")
        if holder == "either":   # mutual clause: the party that changed control (us) gives notice; the other party holds rights
            holder = "us" if e["effect"] == "notice" else "them"
        eid = b.add(f"effect:{i}", "effect", "effect defined outside the file" if e["effect"] == "elsewhere" and t == K.T else
                    f"{str(e['effect']).replace('_', ' ')} ({holder or '?'})", st, ps, inputs={"index": i})
        out.append(b.done(STATUS[st], e["effect"] if st != K.F else "", holder, due, ("coc",), c if st == K.U else "", [eid]))
    return out


# ---------------- assignment ----------------
def assign_reactions(a: dict, s: State) -> list[Reaction]:
    tr = Trace()
    m = tr.fact("mechanism", f"transaction: {(s.mechanism or 'none').replace('_', ' ')}", k(s.mechanism), s.mechanism)
    if not s.mechanism or s.mechanism == "ipo":
        tr.limb("transfer", "no transfer by us", K.F, [m])
        return [tr.done("NOT_TRIGGERED")]
    if s.mechanism == "share_sale":
        cna = tr.atom(a, "coc_not_assignment")
        if a.get("coc_not_assignment"):
            tr.exception("coc_not_assignment", "change of control expressly not an assignment", K.T, [m, cna], say=True)
            return [tr.done("NOT_TRIGGERED", atoms=("coc_not_assignment",))]
        d = tr.atom(a, "deems")
        if "change_of_control" in a.get("deems", []):
            tr.limb("deems", "change of control deemed assignment", K.T, [m, d])
            return [tr.done("TRIGGERED", "consent", "them")]
        tr.limb("deems", "change of control not deemed an assignment", K.F, [m, d], say=False)
        if a.get("merger_exception") and a.get("unless_competitor") and s.buyer_is_competitor:
            tr.limb("competitor", "competitor exception may apply, but a share sale may not be an assignment", K.U,
                    [m, tr.atom(a, "merger_exception"), tr.atom(a, "unless_competitor"),
                     tr.fact("buyer_is_competitor", "buyer is a competitor", K.T, True)])
            return [tr.done("NEEDS_LAWYER", cause="legal_judgement")]
        tr.limb("share_sale", "a share sale is not an assignment by the company", K.F, [m])
        return [tr.done("NOT_TRIGGERED")]
    d = tr.atom(a, "deems")
    if s.mechanism == "share_exchange" and "reorganization" in a.get("deems", []):
        # the company's shareholders swap shares with a new parent: whether that is a "reorganization involving"
        # the company is a reading question, and the two readings give different answers
        tr.limb("deems", "share exchange may or may not be a reorganization involving the company", K.U, [m, d])
        return [tr.done("NEEDS_LAWYER", cause="legal_judgement")]
    hit = s.mechanism in a.get("deems", [])
    ps = [m, d]
    if a.get("merger_exception"):
        exc = not (a.get("unless_competitor") and s.buyer_is_competitor)
        ps.append(tr.exception("merger_exception", "merger exception applies" if exc else "merger exception lifted: buyer is a competitor",
                               k(exc), [tr.atom(a, "merger_exception"), tr.atom(a, "unless_competitor"),
                                        tr.fact("buyer_is_competitor", "buyer is a competitor", k(s.buyer_is_competitor), s.buyer_is_competitor)]))
    if hit and not (a.get("merger_exception") and not (a.get("unless_competitor") and s.buyer_is_competitor)):
        tr.limb("deems", f"{s.mechanism} deemed assignment", K.T, ps)
        return [tr.done("TRIGGERED", "consent", "them", atoms=("deems",))]
    tr.limb("deems", f"{s.mechanism} not deemed an assignment", K.F, ps)
    return [tr.done("NOT_TRIGGERED")]


# ---------------- term and renewal ----------------
def expiry(a: dict) -> date | None:
    if "end_date" in a:
        return date.fromisoformat(a["end_date"])
    if a.get("initial_term") == "redacted" or "start" not in a:
        return None
    n, u = a["initial_term"]
    return add_period(add_period(date.fromisoformat(a["start"]), n, u), 1, "days", -1)


def term_reactions(a: dict, s: State) -> list[Reaction]:
    tr = Trace()
    mode = a.get("mode")
    md = tr.atom(a, "mode")
    if mode == "conditional":
        tr.limb("mode", "renewal depends on facts outside the text", K.U, [md])
        return [tr.done("NEEDS_LAWYER", cause="missing_fact")]
    exp = expiry(a)
    src = [md] + [tr.atom(a, x) for x in ("end_date",) if x in a] + ([tr.atom(a, "start"), tr.atom(a, "initial_term")] if "end_date" not in a else [])
    if exp is None:
        tr.limb("expiry", "term start or length not determinable from the text", K.U, src)
        return [tr.done("NEEDS_LAWYER", cause="missing_document")]
    ex = tr.limb("expiry", f"expiry {exp}", K.T, src, say=False)
    now = tr.fact("as_of", f"as of {s.as_of}", K.T, str(s.as_of))
    if mode == "mutual":
        tr.limb("mode", "renews only by mutual agreement", K.T, [md, ex])
        return [tr.done("TRIGGERED", "expiry", "", exp)]
    n, u = a.get("notice", (None, None))
    if mode == "evergreen":
        if s.as_of <= exp:
            tr.limb("running", "initial term running", K.T, [ex, now])
            return [tr.done("TRIGGERED", "expiry", "", exp)]
        due = add_period(s.as_of, n, u)
        tr.limb("running", "evergreen: notice now ends it after the notice period", K.T if due else K.U, [ex, now, tr.atom(a, "notice")])
        return [tr.done("NEEDS_LAWYER" if due is None else "TRIGGERED", "earliest_exit", "either", due,
                        cause=_unit_cause(u) if due is None else "")]
    no = tr.atom(a, "notice")
    deadline = add_period(exp, n, u, -1) if n is not None else None
    if deadline is None:
        tr.limb("notice", f"notice period unit not determinable ({u})", K.U, [no])
        return [tr.done("NEEDS_LAWYER", cause=_unit_cause(u))]
    dl = tr.limb("deadline", f"notice deadline {deadline}", K.T, [ex, no], say=False)
    kind = "option_deadline" if mode == "option" else "deadline"
    if s.as_of <= deadline:
        tr.limb("before_deadline", f"expiry {exp}", K.T, [dl, now, md])
        return [tr.done("TRIGGERED", kind, a.get("option_holder", "both") if mode == "option" else "both", deadline)]
    tr.limb("before_deadline", f"as of {s.as_of}: notice deadline passed", K.F, [dl, now], say=False)
    if s.as_of <= exp:
        tr.limb("before_expiry", f"expiry {exp}", K.T, [ex, now, md])
        return [tr.done("TRIGGERED", kind + "_lapsed" if mode != "option" else "option_lapsed", "", deadline)]
    if mode == "option":
        tr.limb("after_expiry", "option not exercised (assumed)", K.T, [ex, now, md])
        return [tr.done("TRIGGERED", "expired", "", exp)]
    tr.limb("after_expiry", f"past expiry {exp}", K.T, [ex, now, md])
    return [tr.done("TRIGGERED", "renewed", "", None)]


def _unit_cause(unit) -> str:
    return "missing_document" if unit == "redacted" else "check_extraction"


TEMPLATES = {"coc": coc_reactions, "assign": assign_reactions, "term": term_reactions}


# ---------------- rules on partly known atoms ----------------
# Atoms with a closed list of values can be tried one by one. Numbers, dates and effects cannot: they stay unknown.
POWER = {"triggers": ["ownership_threshold", "retained_stake", "competitor_stake", "management_change", "transaction_type",
                      "open_textured", "parent_direct_ownership", "cross_reference", "undefined"],
         "transaction_types": ["merger", "consolidation", "asset_sale", "reorganization", "share_sale"],
         "carve_outs": ["same_management", "qualifying_ipo", "prior_holders_keep_50", "consented", "non_competitor"]}
CHOICE = {"whose": ["us", "them", "either"], "threshold_op": ["gt", "gte"], "open_texture": ["judgement", "magnitude", "other"],
          "retained_holder": ["sponsors", "original_owners", "principal_owners", "none"],
          "restricted": [True, False], "consent_needed": [True, False],
          "mode": ["auto", "evergreen", "option", "mutual", "conditional", "none"]}
MAX_READINGS = 5000
PCT = {"threshold_pct", "competitor_threshold_pct", "retained_pct", "post_ipo_threshold_pct"}


def _critical(key: str, s: State) -> list:
    """Values worth trying for an unknown number. Rules only compare such numbers with values in the state, so the
    outcome can change only at those values: trying each one, a point just above it, and the extremes covers every case."""
    if key in PCT:
        pts = {s.buyer_pct, s.largest_other_pct, aggregate_change(s), *s.retained_pct.values()}
        return sorted({0.0, 100.0} | pts | {p + 1e-6 for p in pts})
    if key == "qipo_threshold":
        return [0.0, s.ipo_proceeds, s.ipo_proceeds + 1e-6, 1e15]
    return []


def _subsets(items, allow_empty):
    from itertools import combinations
    return [list(c) for n in range(0 if allow_empty else 1, len(items) + 1) for c in combinations(items, n)]


def _plan(template: str, family: dict, s: State):
    """How a family is evaluated: ("direct", reactions) or ("try", blocker keys, values per blocker, known atoms)."""
    blockers = family.get("_unknown", [])
    base = {k: v for k, v in family.items() if not k.startswith("_")}
    if not blockers:
        return "direct", TEMPLATES[template](base, s)
    if template != "term" and not s.mechanism:
        tr = Trace()
        m = tr.fact("mechanism", "transaction: none", K.F, "")
        tr.limb("decided", "event changes no ownership", K.F, [m])
        return "direct", [tr.done("NOT_TRIGGERED")]
    keys = [b.split(" is ")[0] for b in blockers]
    why = "; ".join(blockers)
    def domain(k):
        if k in CHOICE:
            return CHOICE[k]
        if k in POWER:
            return _subsets(POWER[k], k == "carve_outs")
        return _critical(k, s)
    if template == "coc":
        unreached = _effects_unreached(keys, blockers, base, s, domain)
        if unreached:
            return "direct", [unreached]
    if any(not domain(k) or b.endswith("refers_to_missing") for k, b in zip(keys, blockers)):
        missing = any(b.endswith("refers_to_missing") for b in blockers)
        tr = _fork_trace(keys, blockers)
        if template == "coc":       # show the trigger tests the known atoms allow; the outcome stays open on the blockers
            try:
                _coc_trigger(base, s, tr)
            except (KeyError, TypeError, ValueError):
                pass
            tr.why = []
        return "direct", [traced("NEEDS_LAWYER", f"{template}: {why}", tr=tr,
                                   cause=join_causes("missing_document" if missing else "",
                                                     "check_extraction" if not all(b.endswith("refers_to_missing") for b in blockers) else ""))]
    options = [[(k, v) for v in domain(k)] for k in keys]
    n = 1
    for o in options:
        n *= len(o)
    if n > MAX_READINGS:
        return "direct", [traced("NEEDS_LAWYER", f"{template}: too many readings ({why})", cause="check_extraction",
                                 tr=_fork_trace(keys, blockers, {k: len(o) for k, o in zip(keys, options)}))]
    return "try", keys, options, base


def _effects_unreached(keys, blockers, base: dict, s: State, domain) -> Reaction | None:
    """Strong Kleene: false AND unknown is false. When every unresolved atom of a change-of-control clause is part of its
    effects, and the trigger is false under every reading of the other unresolved atoms, the effects are never reached:
    the outcome is no reaction, whatever the effects say. Returns that firm outcome, or None to keep the clause open."""
    rest = [(k, b) for k, b in zip(keys, blockers) if ref(k) != "effects"]
    if len(rest) == len(keys) or any(not domain(k) or b.endswith("refers_to_missing") for k, b in rest):
        return None
    from itertools import product
    options = [[(k, v) for v in domain(k)] for k, _ in rest]
    n = 1
    for o in options:
        n *= len(o)
    if n > MAX_READINGS:
        return None
    with quiet():
        for combo in product(*options):
            try:
                if _coc_trigger({**base, **dict(combo)}, s)[0] != K.F:
                    return None
            except (KeyError, TypeError, ValueError):      # a reading needs a value that is not known
                return None
    first = next(product(*options))
    tr = Trace()
    _coc_trigger({**base, **dict(first)}, s, tr)
    eff = [k for k in keys if ref(k) == "effects"]
    tr.limb("unreached", "the trigger is false under every reading, so the effects (" + ", ".join(eff) + ") are not reached",
            K.F, [x for x in tr.steps if x.startswith(("limb:", "exc:"))])
    return tr.done("NOT_TRIGGERED")


def _try(template: str, base: dict, s: State, combo) -> list[Reaction]:
    try:
        return TEMPLATES[template]({**base, **dict(combo)}, s)
    except (KeyError, TypeError, ValueError):      # this reading needs a number or date that is not known
        return [traced("NEEDS_LAWYER", "a needed value is unknown", cause="check_extraction",
                       tr=_fork_trace([k for k, _ in combo], [f"{k} is tried as {v}" for k, v in combo]))]


def _sig(rs) -> tuple:
    return tuple((r.status, r.kind, r.holder, r.due) for r in rs)


def react(template: str, family: dict, s: State) -> list[Reaction]:
    """Reactions of one compiled family. `_unknown` lists blockers as 'atom is status'. Each blocker with a closed list of values is
    tried with every value; a firm answer is returned only when all readings give the same reactions, else NEEDS_LAWYER."""
    plan = _plan(template, family, s)
    if plan[0] == "direct":
        return plan[1]
    _, keys, options, base = plan
    from itertools import product
    seen = {}
    with quiet():
        for combo in product(*options):
            rs = _try(template, base, s, combo)
            seen.setdefault(_sig(rs), (combo, rs))
    if len(seen) == 1 and _RECORD[0]:
        combo = next(iter(seen.values()))[0]
        seen = {k_: (combo, _try(template, base, s, combo)) for k_ in seen}
    tried = {k: [v for k2, v in o] for k, o in zip(keys, options)}
    if len(seen) == 1:
        return [with_forks(r, tried, "every tried value gives this outcome") for r in next(iter(seen.values()))[1]]
    tr = _fork_trace(keys, [f"{k} is unknown: {len(seen)} different outcomes across {len(tried[k])} values" for k in keys], tried)
    return [traced("NEEDS_LAWYER", f"{template}: depends on {' and '.join(keys)}", tr=tr,
                   cause=join_causes("check_extraction", *(r.cause for _, rs in seen.values() for r in rs)))]


def ref(key: str) -> str:
    """The reviewed atom a compiled key belongs to (the unit and number of one period are one atom)."""
    return {"effect": "effects", "effect_holder": "effects", "unless_consented": "effects", "qipo_test": "qualifying_ipo",
            "qipo_threshold": "qualifying_ipo"}.get(key, key.rsplit("_", 1)[0] if key.endswith(("_n", "_unit")) else key)


def _fork_trace(keys, labels, tried=None) -> Trace:
    """Steps for atoms the compiler could not settle: one fork step per atom, with the values tried."""
    tr = Trace()
    for k_, lab in zip(keys, labels):
        t = (tried or {}).get(k_)
        tr.add("fork:" + ref(k_), "fork", lab, K.U, atoms=(ref(k_),), inputs={"tried": [] if t is None else t})
    return tr


def with_forks(r: Reaction, tried: dict, note: str, combo=None) -> Reaction:
    """The reaction with each atom step for a tried atom turned into a fork step: the value was assumed, not compiled.
    `combo` gives the assumed values of one branch; without it the step says every value was tried."""
    refs = {ref(k_): k_ for k_ in tried}
    out = []
    for st in r.trace:
        if st.kind == "atom" and st.atom_refs and st.atom_refs[0] in refs:
            k_ = refs[st.atom_refs[0]]
            lab = f"{k_} assumed {combo[k_]}" if combo is not None and k_ in combo else f"{k_} unknown: {note}"
            st = replace(st, id="fork:" + st.atom_refs[0], kind="fork", label=lab, value=K.U,
                         inputs=(("assumed", combo.get(k_)) if combo else ("tried", tried[k_]),))
        out.append(st)
    seen = {st.atom_refs[0] for st in out if st.kind == "fork" and st.atom_refs}
    lead = [Step("fork:" + ref(k_), "fork", f"{k_} assumed {combo[k_]}" if combo else f"{k_} unknown: {note}",
                 (("assumed", combo[k_]),) if combo else (("tried", tried[k_]),), K.U, (ref(k_),))
            for k_ in tried if ref(k_) not in seen]     # an atom the rule reads without an atom step of its own
    out = lead + out
    ids = {a.id: b.id for a, b in zip(r.trace, out[len(lead):])}
    out = [replace(st, parents=tuple(dict.fromkeys(ids.get(p, p) for p in st.parents))) for st in out]
    return replace(r, trace=tuple(out))


# ---------------- readings: what each way of resolving an open outcome leads to ----------------
FORK_TEXT = {"open_textured": "if the open standard in the clause is met", "parent_direct_ownership":
             "if ownership through the new holding company breaks the direct-ownership test"}


def _forks(template: str, a: dict, s: State, rs: list[Reaction]):
    """[(reactions, reading text)] for one fully known family. A rule that states two readings gives both; any other open
    outcome (a missing document or fact, an unsure atom) cannot be computed: None."""
    if all(r.status != "NEEDS_LAWYER" for r in rs):
        return [(rs, "")]
    if any(r.status == "NEEDS_LAWYER" and r.cause != "legal_judgement" for r in rs):
        return None
    def reading(text, value, base=None):
        tr = base.copy() if base else Trace()
        tr.add("fork:reading", "fork", text, value, [x for x in tr.steps if x.startswith("limb:")], inputs={"reading": text})
        return tr
    if template == "coc":
        tr = Trace()
        _, why, open_ = _coc_trigger(a, s, tr)
        yes = "; ".join(sorted({FORK_TEXT.get(t, f"if {t} applies") for t in open_}))
        out = [(coc_effects(a, s, K.T, why, tr=reading(yes, K.T, tr)), yes),
               (coc_effects(a, s, K.F, why, tr=reading("otherwise", K.F, tr)), "otherwise")]
    elif template == "assign" and s.mechanism == "share_exchange":
        yes = "if a share exchange counts as a reorganization"
        r = assign_reactions(a, replace(s, mechanism="reorganization"))
        r = [replace(x, trace=tuple(reading(yes, K.T).steps.values()) + x.trace) for x in r]
        out = [(r, yes), ([traced("NOT_TRIGGERED", "share exchange is not a reorganization involving the company",
                                  tr=reading("otherwise", K.F))], "otherwise")]
    elif template == "assign":
        yes = "if the competitor exception reaches a share sale"
        out = [([traced("TRIGGERED", "competitor exception reaches a share sale", "consent", "them", tr=reading(yes, K.T))], yes),
               ([traced("NOT_TRIGGERED", "a share sale is not an assignment by the company", tr=reading("otherwise", K.F))], "otherwise")]
    else:
        return None
    return None if any(r.status == "NEEDS_LAWYER" for x, _ in out for r in x) else out


def readings(template: str, family: dict, s: State):
    """Each distinct outcome of a family in state s, with what leads to it: a list of dicts {reactions, when}, where `when`
    lists the alternatives that give this outcome, each {values: {atom: value}, reading: text}. Blockers are tried value by
    value as in `react`; a rule that states two readings contributes both. None when some reading cannot be computed."""
    plan = _plan(template, family, s)
    if plan[0] == "direct":
        combos = [((), plan[1])]
        base = {k: v for k, v in family.items() if not k.startswith("_")}
    else:
        from itertools import product
        _, keys, options, base = plan
        with quiet():
            combos = [(c, _try(template, base, s, c)) for c in product(*options)]
        tried = {k: [v for _, v in o] for k, o in zip(keys, options)}
    out, first = {}, {}
    with quiet():
        for combo, rs in combos:
            forks = _forks(template, {**base, **dict(combo)}, s, rs)
            if forks is None:
                return None
            for i, (rs2, text) in enumerate(forks):
                o = out.setdefault(_sig(rs2), dict(reactions=rs2, when=[]))
                first.setdefault(_sig(rs2), (combo, i))
                o["when"].append(dict(values=dict(combo), reading=text))
    if _RECORD[0]:          # the reactions of each outcome, with steps, from the first reading that gives it
        for k_, (combo, i) in first.items():
            rs = _try(template, base, s, combo) if combo else combos[0][1]
            rs2 = _forks(template, {**base, **dict(combo)}, s, rs)[i][0]
            out[k_]["reactions"] = [with_forks(r, tried, "", dict(combo)) for r in rs2] if combo else rs2
    return [out[k] for k in sorted(out, key=repr)]
