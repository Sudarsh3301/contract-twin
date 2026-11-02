"""Portfolio run: a fixed scenario library over every contract, exported as schedules, an exposure map and one offline page.

    python -m twin.portfolio simulate        # hand labels play the reviewer -> out/portfolio/reviews_simulated.json
    python -m twin.portfolio [--out out/portfolio/] [--atoms compiled|gold]   # both states: exports + index.html
    python -m twin.portfolio check           # determinism, verbatim citations, causes, worst cases, queue order
    python -m twin.portfolio measure         # reviewer effort on the scenario cases: reasoning-path cards vs single atoms

Two states: "compiled" (compiled atoms with the stored reviews) and "review" (the same plus the simulated review set, kept
apart from the real reviews in the database). Every outcome is Firm, Provisional (an open outcome whose readings can all
be computed; the worst one is shown) or Blocked (some reading needs a document, a fact or an atom that cannot be tried).
Reactions come from the rules only. No model is called.
"""
import argparse
import csv
import io
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from . import compile as comp
from . import review
from .core import State, apply
from .ingest import DB, ROOT
from .rules import CAUSES, CHOICE, PCT, Reaction, join_causes, react, readings

D = "2026-11-02"           # the as-of date every scenario starts from
LIBRARY = [
    dict(id="hold_merger", name="Holding company inserted by merger",
         events=[{"event": "holding_insertion", "mechanism": "merger", "date": D}]),
    dict(id="hold_exchange", name="Holding company inserted by share exchange",
         events=[{"event": "holding_insertion", "mechanism": "share_exchange", "date": D}]),
    dict(id="ipo_200_60", name="IPO $200M, sponsors keep 60%",
         events=[{"event": "ipo", "gross_proceeds_usd": 200_000_000, "sponsor_pct_after": 60, "largest_other_pct_after": 5, "date": D}]),
    dict(id="ipo_100_45", name="IPO $100M, sponsors keep 45%",
         events=[{"event": "ipo", "gross_proceeds_usd": 100_000_000, "sponsor_pct_after": 45, "largest_other_pct_after": 5, "date": D}]),
    dict(id="sale_30", name="30% sold to one buyer", events=[{"event": "share_sale", "buyer_pct": 30, "date": D}]),
    dict(id="sale_50", name="50% sold to one buyer", events=[{"event": "share_sale", "buyer_pct": 50, "date": D}]),
    dict(id="sale_60", name="60% sold to one buyer", events=[{"event": "share_sale", "buyer_pct": 60, "date": D}]),
    dict(id="sale_10_comp", name="10% sold to a competitor",
         events=[{"event": "share_sale", "buyer_pct": 10, "buyer_is_competitor": True, "management_changes": False, "date": D}]),
    dict(id="clock_90", name="Clock +90 days (to 2027-01-31)", events=[{"event": "date_advance", "to": "2027-01-31"}]),
]
for _sc in LIBRARY:
    _sc["templates"] = ["term"] if all(e["event"] == "date_advance" for e in _sc["events"]) else ["coc", "assign"]

SIMULATED = ROOT / "out" / "portfolio" / "reviews_simulated.json"
STATES = {"compiled": "Compiled (no review)", "review": "After review (simulated)"}
CAUSE_LABEL = {"legal_judgement": "Decision for counsel", "missing_document": "Missing document",
               "missing_fact": "Missing fact", "check_extraction": "Check extraction"}

# severity for us; the worst-case plan and the queue order use it
SEV_KIND = {"termination_right": 6, "automatic_termination": 6, "prepayment": 6, "consent": 5, "notice": 4, "service_limitation": 3}
TERM_DEADLINES = {"expiry", "deadline", "option_deadline", "earliest_exit"}
MATERIAL = 5               # consent or worse
SEV_NAME = {6: "termination right", 5: "consent", 4: "notice", 3: "service limitation", 2: "deadline", 1: "other", 0: "none"}
KIND_CLASS = {"termination_right": "termination", "automatic_termination": "termination", "prepayment": "prepayment",
              "consent": "consent", "notice": "notice"}


def state_of(sc: dict) -> State:
    s = State()
    for ev in sc["events"]:
        s = apply(s, ev)
    return s


def sev(r: Reaction) -> int:
    if r.status != "TRIGGERED":
        return 0
    if r.kind in SEV_KIND:
        return SEV_KIND[r.kind]
    return 2 if r.kind in TERM_DEADLINES and r.due and str(r.due) >= D else 1


def sev_all(rs) -> int:
    return max((sev(r) for r in rs), default=0)


def css(r: Reaction) -> str:
    """Colour class of one reaction."""
    if r.status != "TRIGGERED":
        return "none"
    return KIND_CLASS.get(r.kind) or ("deadline" if sev(r) == 2 else "other")


def label(r: Reaction) -> str:
    if r.status != "TRIGGERED":
        return "no reaction"
    holder = {"us": " (us)", "them": " (counterparty)"}.get(r.holder, "")
    return r.kind.replace("_", " ") + holder + (f" by {r.due}" if r.due else "")


def clause_max(t: str, fam: dict | None) -> int:
    """The most severe thing a clause could do once its open atoms are settled: its listed effects (all effects if none
    are known), consent for an assignment clause, a deadline for a term clause."""
    if not fam:
        return 0
    if t == "coc":
        effs = review.get_effects(fam)
        return max((SEV_KIND.get(e.get("effect"), 6 if e.get("effect") == "elsewhere" else 3) for e in effs), default=6)
    if t == "assign":
        return 0 if fam.get("restricted") is False else 5
    return 2


# ---------------- reasons in plain words ----------------
_PLAIN = [
    (r"clause concerns the counterparty's change of control", lambda m: "the clause is about the other party's change of control"),
    (r"buyer ([\d.]+)% vs (gt|gte) ([\d.]+)%", lambda m: f"buyer gets {m[1]}%; the clause needs {'more than' if m[2] == 'gt' else 'at least'} {m[3]}%"),
    (r"post-QIPO: other holder ([\d.]+)% vs > ([\d.]+)% and >= (\w+) ([\d.]+)%",
     lambda m: f"after a qualifying IPO: the largest other holder has {m[1]}% (the clause needs more than {m[2]}% and at least the {m[3]}' {m[4]}%)"),
    (r"(\w+) keep ([\d.]+)% vs required ([\d.]+)%", lambda m: f"{m[1]} keep {m[2]}%; the clause triggers below {m[3]}%"),
    (r"competitor stake 0(\.0)?% vs >= ([\d.]+)%", lambda m: "buyer is not a competitor"),
    (r"competitor stake ([\d.]+)% vs >= ([\d.]+)%", lambda m: f"a competitor gets {m[1]}%; the clause triggers at {m[2]}% or more"),
    (r"management changed: True", lambda m: "management changes"),
    (r"management changed: False", lambda m: "management stays the same"),
    (r"mechanism (\w+) vs \[(.*)\]", lambda m: f"the transaction is a {m[1].replace('_', ' ')}; the clause lists {m[2] or 'no transaction type'}".replace("'", "")),
    (r"counterparty judgement standard", lambda m: "the other party's judgement decides (open standard)"),
    (r"open-textured magnitude, buyer ([\d.]+)%", lambda m: f"a vague size word (such as 'significant') applies; buyer gets {m[1]}%"),
    (r"holding position (\w+)", lambda m: "no new holding company" if m[1] == "none" else f"new holding company {m[1].replace('_', ' ')}"),
    (r"carve-out: (.*)", lambda m: f"exception applies: {m[1].replace('_', ' ')}"),
    (r"(\w+): depends on (.*)", lambda m: f"the outcome depends on {m[2]}, which the compiler could not settle"),
    (r"(\w+): too many readings \((.*)\)", lambda m: f"too many open atoms to try every reading ({m[2]})"),
    (r"(coc|assign|term): (.* is \w+.*)", lambda m: f"the compiler could not settle: {m[2]}"),
]


def plain(reason: str) -> str:
    """A rule's reason in words a reader outside the code understands. Display only: nothing is decided from it."""
    out = []
    for part in reason.split("; "):
        for rx, f in _PLAIN:
            m = re.fullmatch(rx, part)
            if m:
                part = f(m)
                break
        out.append(part)
    return "; ".join(out)


# ---------------- inputs ----------------
def load_gold() -> dict:
    return {k: v for f in ("cases/gold_atoms.json", "cases/gold_atoms_ar.json")
            for k, v in json.loads((ROOT / f).read_text(encoding="utf-8")).items() if not k.startswith("_")}


class Ctx:
    """Everything a run reads: contracts (atom key -> metadata), atoms with reviews applied, reviews, the database.
    `state`: compiled (stored reviews only) or review (stored reviews + the simulated set)."""

    def __init__(self, atoms_kind="compiled", state="compiled"):
        self.con = sqlite3.connect(DB)
        self.gold = atoms_kind == "gold"
        self.state = state
        self.contracts = {}
        for cid, title, lang, path in self.con.execute("SELECT id, title, language, source_path FROM contract ORDER BY id"):
            if cid not in comp.PORTFOLIO:
                continue
            key = review.contract_key(cid, lang)
            self.contracts[key] = dict(cid=cid, title=title, lang=lang, path=path, party=comp.PORTFOLIO[cid]["our_party"],
                                       run="main" if lang == "en" else "main_ar")
        if self.gold:
            atoms = load_gold()
        else:
            atoms = {k: v for f in ("out/compiled_atoms.json", "out/compiled_atoms_ar.json")
                     for k, v in json.loads((ROOT / f).read_text(encoding="utf-8")).items() if not k.startswith("_")}
        self.reviews = [] if self.gold else review.load_reviews(self.con)
        self.sim = json.loads(SIMULATED.read_text(encoding="utf-8")) if state == "review" else None
        if self.sim:
            self.reviews = self.reviews + self.sim["reviews"]
        self.atoms = review.apply_reviews(atoms, self.reviews)
        self.done = review.confirmed(self.reviews)
        self._cited = {}

    def cited(self, key, t, atom):
        """[(sid, text)] the compiler cited for one atom (falls back to the clause, then to keyword hits)."""
        k = (key, t, atom)
        if k not in self._cited:
            c = self.contracts[key]
            fam = self.atoms.get(key, {}).get(t, {})
            self._cited[k] = review.cited_text(self.con, c["cid"], t, atom, fam, "gold" if self.gold else c["run"])
        return self._cited[k]

    def atom_info(self, key, t, atom):
        c, fam = self.contracts[key], self.atoms.get(key, {}).get(t, {})
        rows = self.con.execute(f"SELECT status FROM atom WHERE run=? AND contract_id=? AND template=? AND key IN "
                                f"({','.join('?' * len(review.atom_keys(t, atom)))})",
                                [c["run"], c["cid"], t, *review.atom_keys(t, atom)]).fetchall()
        blocked = atom in review.blocker_atoms(fam)
        k, ok = (0, False) if self.gold else review.agreement(self.con, c["cid"], t, atom, c["run"])
        value = None if blocked else review.get_atom(fam, atom)
        reviewed = (key, t, atom) in self.done
        return dict(value=value, agree=k, support=ok, reviewed=reviewed,
                    status="unknown" if blocked else "/".join(sorted({r[0] for r in rows})) or ("gold" if self.gold else "not compiled"),
                    unsure=atom in review.unsure(fam) and not reviewed,
                    suspicious="a competitor threshold of 0% is suspicious: a reportable (13d-1) stake is about 5%"
                    if atom == "competitor_threshold_pct" and value == 0 else "")


# ---------------- run ----------------
def describe(rds: list) -> list[dict]:
    """Readings as branches, one line each: the outcome and the atom values or stated reading that lead to it.
    A condition shared by every branch is left out; values that cover every branch are left out too."""
    def vals(rd, a):
        return {json.dumps(w["values"].get(a), sort_keys=True) for w in rd["when"]}

    def set_parts(a, mine, every):
        """For a list-valued atom: the items every alternative of this branch includes, or leaves out, unlike the rest."""
        mine, every = [set(json.loads(v)) for v in mine], [set(json.loads(v)) for v in every]
        items = sorted(set().union(*every))
        return [f"{a} includes {e}" for e in items if all(e in m for m in mine) and not all(e in m for m in every)] + \
               [f"{a} leaves out {e}" for e in items if all(e not in m for m in mine) and any(e in m for m in every)]
    atoms = sorted({a for rd in rds for w in rd["when"] for a in w["values"]})
    texts = {w["reading"] for rd in rds for w in rd["when"]}
    out = []
    for rd in rds:
        parts = []
        for a in atoms:
            mine, every = vals(rd, a), set().union(*(vals(x, a) for x in rds))
            if mine == every:
                continue
            listy = all(v.startswith("[") and not v.startswith("[{") for v in every)
            extra = set_parts(a, mine, every) if listy else []
            parts += extra or [f"{a} is " + (" or ".join(sorted(mine)) if len(mine) <= 3 else f"one of {len(mine)} values")]
        mine = {w["reading"] for w in rd["when"]}
        if mine != texts:
            parts += sorted(t for t in mine if t)
        rs = rd["reactions"]
        out.append(dict(label="; ".join(sorted({label(r) for r in rs})), sev=sev_all(rs),
                        when=" and ".join(parts) or "in every reading"))
    return out


def outcome(t: str, fam: dict, s: State) -> dict:
    """Status of one family in one state: firm, provisional (with its readings and the worst one) or blocked."""
    try:
        rs = react(t, fam, s)
    except (KeyError, TypeError, ValueError):     # a value the rules cannot read (e.g. a threshold marked absent)
        rs = [Reaction("NEEDS_LAWYER", reason=f"{t}: a needed value is not stated in this text", cause="missing_document")]
    if review.firm(rs):
        return dict(status="firm", reactions=rs, worst=rs, readings=None)
    try:
        rds = readings(t, fam, s)
    except (KeyError, TypeError, ValueError):
        rds = None
    if rds is None:
        return dict(status="blocked", reactions=rs, worst=None, readings=None)
    worst = max(rds, key=lambda rd: sev_all(rd["reactions"]))["reactions"]      # first most severe in the sorted readings
    return dict(status="provisional", reactions=rs, worst=worst, readings=rds)


def run(library: list, ctx: Ctx) -> list[dict]:
    """One row per scenario x contract x template. `atoms` are the atoms the outcome rests on (load-bearing for a firm
    outcome, the blockers that matter otherwise); `unconfirmed` are the known load-bearing atoms nobody has reviewed, `ask`
    the ones the compiler could not settle (asked as questions, not confirmed)."""
    rows = []
    for sc in library:
        s = state_of(sc)
        for key in sorted(ctx.contracts):
            for t in sc["templates"]:
                fam = ctx.atoms.get(key, {}).get(t)
                if fam is None:
                    continue
                o = outcome(t, fam, s)
                if o["status"] == "firm":
                    used = review.load_bearing(t, fam, s)
                    open_ = [] if ctx.gold else [a for a in used if (key, t, a) not in ctx.done]
                else:
                    used = review.relevant_atoms(t, fam, s) if fam.get("_unknown") else review.logical_atoms(t, fam)
                    open_ = []
                ask = [a for a in open_ if a in review.unsure(fam)]
                causes = join_causes(*(r.cause for r in o["reactions"] if r.status == "NEEDS_LAWYER"))
                assert o["status"] == "firm" or causes, f"an open outcome without a cause: {key} {t} {sc['id']}"
                row = dict(scenario=sc["id"], contract=key, template=t, status=o["status"], atoms=used,
                           unconfirmed=[a for a in open_ if a not in ask], ask=ask, causes=causes.split(",") if causes else [],
                           reactions=[dict(status=r.status, kind=r.kind, holder=r.holder, due=str(r.due) if r.due else "",
                                           reason=plain(r.reason), cause=r.cause) for r in o["reactions"]],
                           sids=sorted({sid for a in used for sid, _ in ctx.cited(key, t, a)}))
                if o["status"] == "firm" and fam.get("_unknown"):
                    row["tried"] = review.blocker_atoms(fam)      # firm in every reading; the reason shows the first one tried
                if o["status"] != "firm":
                    row["missing"] = "; ".join(sorted({plain(r.reason) for r in o["reactions"] if r.status == "NEEDS_LAWYER"}))
                if o["status"] == "provisional":
                    row["branches"] = describe(o["readings"])
                    w = o["worst"]
                    row["worst"] = [dict(status=r.status, kind=r.kind, holder=r.holder, due=str(r.due) if r.due else "",
                                         reason=plain(r.reason)) for r in w]
                    row["worst_sev"] = sev_all(w)
                    fork = any(w2["reading"] for rd in o["readings"] for w2 in rd["when"])
                    row["clears"] = (len(review.relevant_atoms(t, fam, s)) if fam.get("_unknown") else 0) + fork
                rows.append(row)
    return rows


def shown(row: dict) -> list[dict]:
    """The reactions a row puts on the map: the firm ones, or the worst case of a provisional outcome."""
    return row["reactions"] if row["status"] == "firm" else row.get("worst", [])


def _sev_row(x: dict) -> int:
    return sev(Reaction(x["status"], x["kind"], x["holder"], x["due"] or None))


def cells(rows: list[dict]) -> dict:
    by = {}
    for r in rows:
        by.setdefault(r["scenario"], {}).setdefault(r["contract"], []).append(r)
    out = {}
    for sc, cs in by.items():
        out[sc] = {}
        for k, rs in sorted(cs.items()):
            st = "blocked" if any(r["status"] == "blocked" for r in rs) else "provisional" if any(r["status"] == "provisional" for r in rs) else "firm"
            reacts = [x for r in rs for x in shown(r)]
            top = max(reacts, key=_sev_row, default=None)
            lbls = sorted({label(Reaction(x["status"], x["kind"], x["holder"])) for x in reacts if x["status"] == "TRIGGERED"},
                          key=lambda l: -max(_sev_row(x) for x in reacts if label(Reaction(x["status"], x["kind"], x["holder"])) == l))
            out[sc][k] = dict(status=st, sev=_sev_row(top) if top else 0,
                              cls=css(Reaction(top["status"], top["kind"], top["holder"], top["due"] or None)) if top else "none",
                              labels=lbls or (["no reaction"] if reacts else []),
                              causes=[c for c in CAUSES if any(c in r["causes"] for r in rs)],
                              clears=sum(r.get("clears", 0) for r in rs if r["status"] == "provisional"),
                              pending=any(r["unconfirmed"] or r["ask"] for r in rs),
                              missing="; ".join(r["missing"] for r in rs if r["status"] == "blocked"))
    return out


def who(holder: str, party: str) -> str:
    return {"us": f"our party ({party})", "them": "counterparty", "either": "either party", "both": "both parties"}.get(holder, "-")


# ---------------- review queue ----------------
def answer_word(v) -> str:
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return "; ".join(f"{e['effect'].replace('_', ' ')} ({e.get('effect_holder', '?')})" for e in v)
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "none"
    return str(v)


def impact(outs: list) -> str:
    """'4 outcomes become firm (3 termination rights, 1 notice)' for the outcomes one answer leaves."""
    firm = [rs for rs in outs if rs is not None and review.firm(rs)]
    kinds = Counter(label(Reaction(r.status, r.kind)) for rs in firm for r in rs if r.status == "TRIGGERED")
    kinds["no reaction"] = sum(all(r.status != "TRIGGERED" for r in rs) for rs in firm)
    what = ", ".join(f"{n} {k}{'s' if n > 1 and k != 'no reaction' else ''}"
                     for k, n in sorted(kinds.items(), key=lambda x: (-x[1], x[0])) if n)
    gone = sum(rs is None for rs in outs)
    head = f"{len(firm)} of {len(outs)} become firm: {what}" if firm else f"none of {len(outs)} become firm"
    return head + (f"; clause removed in {gone}" if gone else "")


def queue(ctx: Ctx, library: list) -> list[dict]:
    """Questions to answer: unknown atoms that block an outcome, and atoms the compiler could not settle that a firm outcome
    rests on. Severity first (the most severe outcome an answer could produce), then outcomes made firm, outcomes touched,
    contract name."""
    states = {sc["id"]: state_of(sc) for sc in library}
    qs = [q for q in review.questions(ctx.atoms, library, ctx.con, run="main", reviews=ctx.reviews) if q.kind == "blocking"]
    out = []
    for q in qs:
        fam = ctx.atoms[q.contract][q.template]
        free = isinstance(q.preview[0]["answer"], str) and q.preview[0]["answer"].startswith("(")
        lines, worst, by_sc, detail = {}, 0, {}, {}
        for p in ([] if free else q.preview):
            after = review.apply_reviews({q.contract: {q.template: fam}},
                                         [dict(contract_id=q.contract, template=q.template, atom=q.atom, value=p["answer"])])
            fam2 = after[q.contract].get(q.template)
            outs, where = [], {}
            for sid in q.pairs:
                rs = review.run_pair(after, q.contract, q.template, states[sid])
                outs.append(rs)
                v = 0 if rs is None else sev_all(rs) if review.firm(rs) else clause_max(q.template, fam2)
                by_sc[sid] = max(by_sc.get(sid, 0), v)
                what = "clause removed" if rs is None else "; ".join(sorted({label(r) if r.status != "NEEDS_LAWYER" else "still open" for r in rs}))
                where.setdefault(what, []).append(sid)
            line = impact(outs)
            lines.setdefault(line, []).append(answer_word(p["answer"]))
            detail.setdefault(line, f"for {answer_word(p['answer'])}: " + "; ".join(f"{w} in {', '.join(s)}" for w, s in where.items()))
        if free:
            for sid in q.pairs:
                by_sc[sid] = clause_max(q.template, fam)
            lines = {f"{len(q.pairs)} outcome(s) can be computed once the value is known": ["Any value"]}
            detail = {}
        worst = max(by_sc.values(), default=0)
        out.append(dict(id=q.id, contract=q.contract, template=q.template, atom=q.atom, text=q.text, allowed=q.allowed,
                        cited=q.cited or ctx.cited(q.contract, q.template, q.atom), pairs=q.pairs, firm_pairs=q.firm_pairs,
                        unsure=q.current is None and q.atom in review.unsure(fam),
                        preview=[dict(answers=a, line=l, detail=detail.get(l, "")) for l, a in lines.items()], sev=worst, sev_by=by_sc))
    out.sort(key=lambda x: (-x["sev"], -x["firm_pairs"], -len(x["pairs"]), x["contract"], x["template"], x["atom"]))
    return out


def path_cards(rows: list[dict], ctx: Ctx, info: dict) -> list[dict]:
    """One reasoning-path card per pending outcome: every unconfirmed known load-bearing atom of it, to confirm together.
    Outcomes of one clause that rest on the same atoms share a card. Rank: severity (the most severe outcome a correction
    of any atom could produce), then weakest compile evidence, most outcomes, contract. A card whose atoms earlier cards
    already confirm is folded into the last of those cards, so the count shown is the count a reviewer opens."""
    states = {sc["id"]: state_of(sc) for sc in LIBRARY}
    cards = {}
    for r in rows:
        if r["unconfirmed"]:
            c = cards.setdefault((r["contract"], r["template"], tuple(r["unconfirmed"])), dict(outcomes={}))
            c["outcomes"].setdefault(r["scenario"], set()).update(label(Reaction(x["status"], x["kind"], x["holder"], x["due"] or None))
                                                                 for x in r["reactions"])
    out = []
    for (key, t, atoms), c in cards.items():
        fam = ctx.atoms[key][t]
        by_sc = {}
        for sid in c["outcomes"]:
            s = states[sid]
            v = sev_all(react(t, fam, s))
            for a in atoms:
                for f in review.variants_of(t, fam, a, s):
                    try:
                        rs = react(t, f, s) if f is not None else []
                    except (KeyError, TypeError, ValueError):     # this variant needs a value it does not have: still open
                        rs = [Reaction("NEEDS_LAWYER")]
                    v = max(v, sev_all(rs) if review.firm(rs) else clause_max(t, f))
            by_sc[sid] = v
        weakest = min((info[f"{key}|{t}|{a}"]["agree"], info[f"{key}|{t}|{a}"]["support"]) for a in atoms)
        out.append(dict(contract=key, template=t, atoms=list(atoms), weakest=list(weakest), sev=max(by_sc.values()), sev_by=by_sc,
                        outcomes=[dict(scenario=s, label="; ".join(sorted(ls))) for s, ls in sorted(c["outcomes"].items())]))
    out.sort(key=lambda c: (-c["sev"], c["weakest"][0] >= 3 and c["weakest"][1], c["weakest"][0], -len(c["outcomes"]), c["contract"], c["template"], c["atoms"]))
    kept, owner = [], {}
    for c in out:
        atoms = [(c["contract"], c["template"], a) for a in c["atoms"]]
        if all(a in owner for a in atoms):
            host = kept[max(owner[a] for a in atoms)]
            host["also"] = host.get("also", []) + c["outcomes"]
            for sid, v in c["sev_by"].items():
                host["sev_by"][sid] = max(host["sev_by"].get(sid, 0), v)
            continue
        for a in atoms:
            owner.setdefault(a, len(kept))
        kept.append(c)
    return kept


def card_counts(cards: list[dict]) -> dict:
    atoms = {(c["contract"], c["template"], a) for c in cards for a in c["atoms"]}
    return dict(pending_outcomes=sum(len(c["outcomes"]) + len(c.get("also", [])) for c in cards), cards=len(cards),
                single_atom_confirmations=len(atoms), clauses=len({(c["contract"], c["template"]) for c in cards}))


# ---------------- one state ----------------
def case_safety(ctx: Ctx) -> dict:
    """The scenario cases on this state's atoms: correct / wrong firm / abstain / pending (gold atoms as the expectation)."""
    from tests import review_oracle as ro
    from tests import score_cases as scs
    atoms = {k: v for k, v in ctx.atoms.items() if not k.startswith("ar:")}
    if ctx.gold:
        return ro.score(atoms)[0]
    qs = review.questions(atoms, review.case_scenarios(scs.cases), ctx.con, reviews=ctx.reviews)
    return ro.score(atoms, {p for q in qs if q.kind == "confirm" for p in q.pairs})[0]


def build_state(ctx: Ctx, sents: dict, library=LIBRARY) -> dict:
    rows = run(library, ctx)
    cs = cells(rows)
    info = {f"{r['contract']}|{r['template']}|{a}": None for r in rows for a in r["atoms"]}
    info = {k: ctx.atom_info(*k.split("|")) for k in sorted(info)}
    qs = queue(ctx, library)
    cards = path_cards(rows, ctx, info)

    def sids(key, pairs):           # sentences are stored once per contract; rows, cards and questions keep the ids
        for sid, t in pairs:
            sents.setdefault(key, {})[str(sid)] = t
        return sorted({sid for sid, _ in pairs})
    for r in rows:
        for a in r["atoms"]:
            sids(r["contract"], ctx.cited(r["contract"], r["template"], a))
    for c in cards:
        c["sids"] = sids(c["contract"], [x for a in c["atoms"] for x in ctx.cited(c["contract"], c["template"], a)])
    for q in qs:
        q["sids"] = sids(q["contract"], q.pop("cited"))
    summaries = {}
    for sc in library:
        mine = cs.get(sc["id"], {}).values()
        n = Counter(c["status"] for c in mine)
        items = [x for x in qs + cards if sc["id"] in x["sev_by"]]
        summaries[sc["id"]] = dict(
            firm=n["firm"], provisional=n["provisional"], blocked=n["blocked"],
            counsel=sum("legal_judgement" in c["causes"] for c in mine),
            react=sum(c["sev"] > 0 for c in mine), consent=sum(c["cls"] == "consent" for c in mine),
            notice=sum(c["cls"] == "notice" for c in mine), termination=sum(c["cls"] in ("termination", "prepayment") for c in mine),
            open_questions=sum(sc["id"] in q["sev_by"] for q in qs), pending=sum(sc["id"] in c["sev_by"] for c in cards),
            material_open=sum(x["sev_by"][sc["id"]] >= MATERIAL for x in items))
    nl = [x for r in rows for x in r["reactions"] if x["status"] == "NEEDS_LAWYER"]
    outs = Counter(r["status"] for r in rows)
    sim = ctx.sim["stats"] if ctx.sim else {}
    header = dict(cards_answered=sim.get("cards", 0), questions_answered=sim.get("answers", 0),
                  firm=outs["firm"], provisional=outs["provisional"], blocked=outs["blocked"],
                  counsel=sum("legal_judgement" in r["causes"] for r in rows), outcomes=len(rows),
                  cases=case_safety(ctx), hand_labels=sim.get("hand_labels"))
    return dict(label=STATES[ctx.state] if not ctx.gold else "Gold atoms", header=header, summaries=summaries, cells=cs, rows=rows,
                atominfo=info, questions=qs, cards=cards, effort=card_counts(cards),
                causes={c: sum(c in x["cause"].split(",") for x in nl) for c in CAUSES},
                main_cause={c: sum(x["cause"].split(",")[0] == c for x in nl) for c in CAUSES}, needs_lawyer=len(nl))


def build(atoms_kind="compiled", library=LIBRARY) -> dict:
    """Everything the exports and the page need, from deterministic runs of each state."""
    names = ["compiled"] if atoms_kind == "gold" else ["compiled"] + (["review"] if SIMULATED.exists() else [])
    sents, states, ctx = {}, {}, None
    for name in names:
        ctx = Ctx(atoms_kind, name)
        states[name] = build_state(ctx, sents, library)
    return dict(as_of=D, atoms=atoms_kind, sentences=sents, states=states,
                scenarios=[dict(id=sc["id"], name=sc["name"], templates=sc["templates"], events=sc["events"]) for sc in library],
                contracts={k: dict(title=c["title"], party=c["party"], lang=c["lang"]) for k, c in sorted(ctx.contracts.items())},
                cause_label=CAUSE_LABEL, sev_name={str(k): v for k, v in SEV_NAME.items()})


# ---------------- exports ----------------
def schedule_csv(data: dict, st: dict, sc_id: str) -> str:
    """Work per scenario: firm triggered reactions, provisional worst cases (flagged), blocked outcomes (what is missing)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["status", "contract", "title", "our_party", "template", "reaction", "who_acts", "due", "unconfirmed_atoms",
                "open_because", "reason", "clause"])
    for r in st["rows"]:
        if r["scenario"] != sc_id:
            continue
        c = data["contracts"][r["contract"]]
        clause = " … ".join(f"[{sid}] {data['sentences'][r['contract']][str(sid)]}" for sid in r["sids"])
        because = ", ".join(CAUSE_LABEL[x] for x in r["causes"])
        if r["status"] == "blocked":
            w.writerow(["blocked", r["contract"], c["title"], c["party"], r["template"], "", "", "", "", because, r["missing"], clause])
            continue
        for x in shown(r):
            if x["status"] == "TRIGGERED":
                w.writerow(["firm" if r["status"] == "firm" else "provisional (worst case)", r["contract"], c["title"], c["party"],
                            r["template"], x["kind"], who(x["holder"], c["party"]), x["due"] or "no date in the clause",
                            " ".join(r["unconfirmed"] + r["ask"]), because, x["reason"], clause])
    return buf.getvalue()


def write(data: dict, out: Path) -> None:
    for name, st in data["states"].items():
        d = out if name == "compiled" else out / "after_review"
        d.mkdir(parents=True, exist_ok=True)
        for sc in data["scenarios"]:
            (d / f"schedule_{sc['id']}.csv").write_bytes(schedule_csv(data, st, sc["id"]).encode("utf-8-sig"))
        exposure = dict(as_of=data["as_of"], atoms=data["atoms"], state=st["label"], header=st["header"],
                        scenarios=data["scenarios"], contracts=data["contracts"], cells=st["cells"],
                        outcomes=[{k: r[k] for k in r if k not in ("sids", "atoms", "tried")} for r in st["rows"]])
        (d / "exposure.json").write_bytes(json.dumps(exposure, indent=1, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    page = (Path(__file__).parent / "portfolio.html").read_text(encoding="utf-8")
    def nest(rows):                 # rows by scenario and contract, without repeating the two keys
        out = {}
        for r in rows:
            out.setdefault(r["scenario"], {}).setdefault(r["contract"], []).append({k: v for k, v in r.items() if k not in ("scenario", "contract")})
        return out
    slim = {**data, "states": {n: {**st, "rows": nest(st["rows"]),
                                   "cells": {sc: {k: {x: v for x, v in c.items() if x != "missing"} for k, c in cs.items()}
                                             for sc, cs in st["cells"].items()}}
                               for n, st in data["states"].items()}}        # the page reads what is missing from the rows
    blob = json.dumps(slim, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    (out / "index.html").write_bytes(page.replace("/*DATA*/null", blob).encode("utf-8"))


# ---------------- review walk: gold atoms as the reviewer ----------------
ISO = re.compile(r"\d{4}-\d\d-\d\d")


def readable(atom: str, v) -> bool:
    """Is a hand-labelled value one the rules can use? The labels also hold status words (silent, absent, on_execution,
    refers_to_missing) where the text gives no value; such a label answers nothing, so the question stays open."""
    if atom in PCT or atom == "qipo_threshold":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if atom in ("start", "end_date"):
        return isinstance(v, str) and bool(ISO.fullmatch(v)) or isinstance(v, dict) and bool(ISO.fullmatch(str(v.get("end_date"))))
    if atom in ("initial_term", "notice", "renewal_term"):
        return v == "redacted" or isinstance(v, dict) and bool(ISO.fullmatch(str(v.get("end_date")))) or \
            isinstance(v, list) and len(v) == 2 and isinstance(v[0], (int, float))
    if atom in CHOICE:
        return v in CHOICE[atom]
    return not isinstance(v, str)


# ---------------- review walk: gold atoms as the reviewer ----------------
def walk(start: dict, scenarios: list, base_reviews: list, con, score_cases=False) -> tuple[list, Counter, int]:
    """Blocking questions answered one by one; load-bearing atoms confirmed one reasoning-path card at a time (every
    unconfirmed known load-bearing atom of one pending outcome). The gold atoms in tests.review_oracle judge each item.
    Returns (new reviews, counts, worst wrong-firm count on the scenario cases if `score_cases`)."""
    from tests import review_oracle as ro
    reviews, skipped, n, worst = [], set(), Counter(), 0
    by_id = {x["id"]: x for x in scenarios}

    def judge(q, fam):
        """The gold judgement, or None when the gold gives no value the rules can read (e.g. a threshold marked absent)."""
        j = ro.judge(q, fam)
        return j if j is None or j[0] == "confirm" or j[1] == "no" or readable(q.atom, j[1]) else None

    def store(q, kind, v):
        if v == "no" and q.atom != "present":
            reviews.append(dict(contract_id=q.contract, template=q.template, atom="present", value="no", reviewer="gold",
                                kind="answer" if kind == "answer" else "correct"))
        else:
            for at, v2 in review.expand(q.atom, v) if kind != "confirm" else [(q.atom, v)]:
                reviews.append(dict(contract_id=q.contract, template=q.template, atom=at, value=v2, reviewer="gold", kind=kind))
        n[kind] += 1

    for _ in range(3000):
        atoms = review.apply_reviews(start, reviews)
        qs = review.questions(atoms, scenarios, con, reviews=base_reviews + reviews)
        if score_cases:
            pend = {p for q in qs if q.kind == "confirm" for p in q.pairs}
            worst = max(worst, ro.score(atoms, pend)[0]["wrong firm"])
        todo = [q for q in qs if q.id not in skipped]
        if not todo:
            break
        q = todo[0]
        fam = atoms.get(q.contract, {}).get(q.template, {})
        if q.kind == "blocking":
            j = judge(q, fam)
            if j is None:
                skipped.add(q.id)
                n["skipped"] += 1
            else:
                store(q, *j)
            continue
        done = review.confirmed(base_reviews + reviews)
        card = [a for a in review.load_bearing(q.template, fam, state_of(by_id[q.pairs[0]])) if (q.contract, q.template, a) not in done]
        judged = 0
        for x in qs:
            if x.kind == "confirm" and x.contract == q.contract and x.template == q.template and x.atom in card:
                j = judge(x, fam)
                if j is None:
                    skipped.add(x.id)
                    n["skipped"] += 1
                else:
                    store(x, *j)
                    judged += 1
        if judged:
            n["cards"] += 1
        else:
            skipped.add(q.id)
    return reviews, n, worst


def simulate() -> dict:
    """The simulated review set for the portfolio library: hand labels (English and Arabic gold atoms) answer and confirm,
    only on the clauses they label. Other clauses stay as compiled. Written to out/portfolio/reviews_simulated.json."""
    from tests import review_oracle as ro
    gold = load_gold()
    ro.GOLD = gold                    # the hand labels play the reviewer, Arabic ones included
    ctx = Ctx()
    hand = {(k, t) for k, fams in gold.items() for t in fams if not t.startswith("_") and t in ctx.atoms.get(k, {})}
    start = {k: {t: f for t, f in fams.items() if (k, t) in hand} for k, fams in ctx.atoms.items()}
    start = {k: v for k, v in start.items() if v}
    reviews, n, _ = walk(start, LIBRARY, ctx.reviews, ctx.con)
    labelled = {k for k, _ in hand}
    stats = dict(answers=n["answer"], cards=n["cards"], confirmed=n["confirm"], corrected=n["correct"], skipped=n["skipped"],
                 hand_labels=dict(contracts=len(labelled), clauses=len(hand),
                                  contracts_without=len([k for k in ctx.contracts if k not in labelled]),
                                  clauses_without=sum(len(f) for f in ctx.atoms.values()) - len(hand)))
    res = dict(_note="Reviewer answers simulated from hand labels (cases/gold_atoms*.json); not a lawyer's review. "
                     "Never stored in the review table.", stats=stats, reviews=reviews)
    SIMULATED.parent.mkdir(parents=True, exist_ok=True)
    SIMULATED.write_bytes(json.dumps(res, indent=1, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return stats


def measure() -> dict:
    """Walk the scenario cases from the compiled start with reasoning-path cards and compare with the atom-at-a-time walk."""
    from tests import review_oracle as ro
    from tests import score_cases as scs
    start = ro.start_atoms("compiled")
    con = sqlite3.connect(DB)
    reviews, n, worst = walk(start, review.case_scenarios(scs.cases), [], con, score_cases=True)
    atoms = review.apply_reviews(start, reviews)
    pend = {p for q in review.questions(atoms, review.case_scenarios(scs.cases), con, reviews=reviews) if q.kind == "confirm" for p in q.pairs}
    atomwise = ro.experiment("compiled")
    return dict(cases=len(scs.cases), card_walk=dict(answers=n["answer"], cards=n["cards"], atoms_confirmed=n["confirm"],
                                                     atoms_corrected=n["correct"], end=ro.score(atoms, pend)[0], max_wrong_firm=worst),
                atom_walk=dict(totals=atomwise["totals"], max_correct=atomwise["max_correct"], max_wrong_firm=atomwise["max_wrong_firm"]))


# ---------------- checks ----------------
def verbatim_errors(data: dict, ctx: Ctx, st: dict) -> list[str]:
    """Every triggered reaction on the map (firm, or the worst case of a provisional outcome) cites at least one sentence,
    and each cited sentence is the exact text at its offsets in the source file."""
    errs = []
    src = {k: (ROOT / c["path"]).read_bytes().decode("utf-8") for k, c in ctx.contracts.items()}
    spans = {(cid, sid): (a, b) for cid, sid, a, b in ctx.con.execute("SELECT contract_id, sid, char_start, char_end FROM sentence")}
    for r in st["rows"]:
        if not any(x["status"] == "TRIGGERED" for x in shown(r)):
            continue
        if not r["sids"]:
            errs.append(f"{r['scenario']} {r['contract']} {r['template']}: no cited sentence")
        for sid in r["sids"]:
            a, b = spans[(ctx.contracts[r["contract"]]["cid"], sid)]
            if src[r["contract"]][a:b] != data["sentences"][r["contract"]][str(sid)]:
                errs.append(f"{r['contract']} [{sid}] not verbatim")
    return errs


def worst_errors(ctx: Ctx, st: dict) -> list[str]:
    """Recompute every provisional outcome from readings() and check the shown worst case is the most severe branch."""
    errs, states = [], {sc["id"]: state_of(sc) for sc in LIBRARY}
    for r in st["rows"]:
        if r["status"] != "provisional":
            continue
        rds = readings(r["template"], ctx.atoms[r["contract"]][r["template"]], states[r["scenario"]])
        top = max(sev_all(rd["reactions"]) for rd in rds)
        if r["worst_sev"] != top or sev_all([Reaction(x["status"], x["kind"], x["holder"], x["due"] or None) for x in r["worst"]]) != top:
            errs.append(f"{r['scenario']} {r['contract']} {r['template']}: worst {r['worst_sev']} != {top}")
    return errs


def check(out: Path) -> None:
    import tempfile
    assert plain("competitor stake 0% vs >= 0.0%") == "buyer is not a competitor"
    assert plain("buyer 60% vs gte 50%; management changed: True") == "buyer gets 60%; the clause needs at least 50%; management changes"
    assert plain("something new") == "something new"
    assert sev(Reaction("TRIGGERED", "prepayment")) == sev(Reaction("TRIGGERED", "termination_right")) > sev(Reaction("TRIGGERED", "consent"))
    assert impact([[Reaction("TRIGGERED", "notice")], [Reaction("NOT_TRIGGERED")], [Reaction("NEEDS_LAWYER")], None]) == \
        "2 of 4 become firm: 1 no reaction, 1 notice; clause removed in 1"
    rd = lambda kind, tt: dict(reactions=[Reaction("TRIGGERED" if kind else "NOT_TRIGGERED", kind)],
                              when=[dict(values={"transaction_types": t}, reading="") for t in tt])
    br = describe([rd("", [["asset_sale"], []]), rd("termination_right", [["merger"], ["merger", "asset_sale"]])])
    assert [b["when"] for b in br] == ["transaction_types leaves out merger", "transaction_types includes merger"], br
    assert not SIMULATED.exists() or json.loads(SIMULATED.read_text(encoding="utf-8"))["reviews"], "the simulated set is not empty"
    data = build()
    for name, st in data["states"].items():
        ctx = Ctx(state=name)
        errs = verbatim_errors(data, ctx, st) + worst_errors(ctx, st)
        assert not errs, errs[:5]
        nl = [x for r in st["rows"] for x in r["reactions"] if x["status"] == "NEEDS_LAWYER"]
        assert all(x["cause"] for x in nl), "every open reaction has a cause"
        assert st["effort"]["cards"] == len(st["cards"]), "one number for cards"
        for items in (st["questions"], st["cards"]):
            if any(x["sev"] == 6 for x in items):
                assert items[0]["sev"] == 6, "the first item could cause a termination right or a prepayment"
        assert not any(a in review.unsure(ctx.atoms[c["contract"]][c["template"]]) and (c["contract"], c["template"], a) not in ctx.done
                       for c in st["cards"] for a in c["atoms"]), "a confirm card holds only atoms the compiler settled"
        print(f"{name}: {len(nl)} open reactions, causes {st['causes']}; {sum(r['status'] == 'provisional' for r in st['rows'])} "
              f"provisional worst cases match readings(); cases {st['header']['cases']}")
    if "review" in data["states"]:
        assert data["states"]["review"]["header"]["cases"]["wrong firm"] == 0, "after review: no wrong firm answer on the cases"
    with tempfile.TemporaryDirectory() as tmp:
        write(build(), Path(tmp))
        write(data, out)
        names = sorted(str(p.relative_to(tmp)) for p in Path(tmp).rglob("*") if p.is_file())
        same = [n for n in names if (Path(tmp) / n).read_bytes() == (out / n).read_bytes()]
        assert same == names, sorted(set(names) - set(same))
    print(f"portfolio check ok: {len(names)} files (both states, page included) byte-identical across two runs")


def main():
    ap = argparse.ArgumentParser(prog="python -m twin.portfolio")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "check", "measure", "simulate"])
    ap.add_argument("--out", default="out/portfolio/")
    ap.add_argument("--atoms", default="compiled", choices=["compiled", "gold"])
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    out = ROOT / a.out
    if a.cmd == "check":
        return check(out)
    if a.cmd == "simulate":
        return print(simulate())
    if a.cmd == "measure":
        res = measure()
        out.mkdir(parents=True, exist_ok=True)
        (out / "effort.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        return print(json.dumps(res, indent=1))
    data = build(a.atoms)
    write(data, out)
    for name, st in data["states"].items():
        print(f"== {st['label']}: {st['header']}")
        for sc in data["scenarios"]:
            s = st["summaries"][sc["id"]]
            print(f"  {sc['id']:14} {s['firm']} firm, {s['provisional']} provisional (worst case), {s['blocked']} blocked; "
                  f"{s['counsel']} decisions for counsel; open questions {s['open_questions']}, pending {s['pending']}, material open {s['material_open']}")
        print(f"  cards {st['effort']}; open reactions {st['needs_lawyer']}: main cause {st['main_cause']}, any cause {st['causes']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
