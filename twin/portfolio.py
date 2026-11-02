"""Portfolio run: a fixed scenario library over every contract, exported as schedules, an exposure map and one offline page.

    python -m twin.portfolio [--out out/portfolio/] [--atoms compiled|gold]   # run + exports + index.html
    python -m twin.portfolio check                                           # determinism + verbatim-citation check
    python -m twin.portfolio measure                                         # reviewer effort: reasoning-path cards vs single atoms

Atoms are the compiled atoms with the stored reviews laid over them (or the hand-labelled gold atoms, for comparison).
Reactions come from the rules only. No model is called.
"""
import argparse
import csv
import io
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from . import compile as comp
from . import review
from .core import State, apply
from .ingest import DB, ROOT
from .rules import Reaction, react

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

# cell classes, most severe first; a cell shows every class it has and is coloured by the first
CLASSES = ["termination", "lawyer", "consent", "prepayment", "notice", "deadline", "other", "none"]
KIND_CLASS = {"termination_right": "termination", "automatic_termination": "termination", "consent": "consent",
              "prepayment": "prepayment", "notice": "notice"}
TERM_DEADLINES = {"expiry", "deadline", "option_deadline", "earliest_exit"}


def state_of(sc: dict) -> State:
    s = State()
    for ev in sc["events"]:
        s = apply(s, ev)
    return s


# ---------------- inputs ----------------
class Ctx:
    """Everything a run reads: contracts (atom key -> metadata), atoms with reviews applied, reviews, the database."""

    def __init__(self, atoms_kind="compiled"):
        self.con = sqlite3.connect(DB)
        self.gold = atoms_kind == "gold"
        self.contracts = {}
        for cid, title, lang, path in self.con.execute("SELECT id, title, language, source_path FROM contract ORDER BY id"):
            if cid not in comp.PORTFOLIO:
                continue
            key = comp.gold_key(cid) if lang == "en" else comp.PORTFOLIO[cid]["src"]
            self.contracts[key] = dict(cid=cid, title=title, lang=lang, path=path, party=comp.PORTFOLIO[cid]["our_party"],
                                       run="main" if lang == "en" else "main_ar")
        files = ["cases/gold_atoms.json", "cases/gold_atoms_ar.json"] if self.gold else ["out/compiled_atoms.json", "out/compiled_atoms_ar.json"]
        atoms = {k: v for f in files for k, v in json.loads((ROOT / f).read_text(encoding="utf-8")).items() if not k.startswith("_")}
        self.reviews = [] if self.gold else review.load_reviews(self.con)
        self.atoms = review.apply_reviews(atoms, self.reviews)
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
        return dict(value=None if blocked else review.get_atom(fam, atom),
                    status="unknown" if blocked else "/".join(sorted({r[0] for r in rows})) or ("gold" if self.gold else "not compiled"),
                    agree=k, support=ok, reviewed=(key, t, atom) in review.confirmed(self.reviews))


# ---------------- run ----------------
def run(library: list, ctx: Ctx) -> list[dict]:
    """One row per reaction: scenario x contract x template. `atoms` are the atoms the outcome rests on (load-bearing for a
    firm outcome, the blockers that matter for NEEDS_LAWYER); `unconfirmed` are the load-bearing ones nobody has reviewed."""
    done = review.confirmed(ctx.reviews)
    rows = []
    for sc in library:
        s = state_of(sc)
        for key in sorted(ctx.contracts):
            for t in sc["templates"]:
                fam = ctx.atoms.get(key, {}).get(t)
                if fam is None:
                    continue
                try:
                    rs = react(t, fam, s)
                except (KeyError, TypeError, ValueError):     # a value the rules cannot read (e.g. a threshold marked absent)
                    rs = [Reaction("NEEDS_LAWYER", reason=f"{t}: a needed value cannot be read")]
                if review.firm(rs):
                    used = review.load_bearing(t, fam, s)
                    todo = [] if ctx.gold else [a for a in used if (key, t, a) not in done]
                else:
                    used = review.relevant_atoms(t, fam, s) if fam.get("_unknown") else review.logical_atoms(t, fam)
                    todo = []
                sids = sorted({sid for a in used for sid, _ in ctx.cited(key, t, a)})
                for r in rs:
                    rows.append(dict(scenario=sc["id"], contract=key, template=t, status=r.status, kind=r.kind, holder=r.holder,
                                     due=str(r.due) if r.due else "", reason=r.reason, atoms=used, unconfirmed=todo, sids=sids))
    return rows


def cell_classes(rs: list[dict]) -> list[str]:
    out = set()
    for r in rs:
        if r["status"] == "NEEDS_LAWYER":
            out.add("lawyer")
        elif r["status"] == "TRIGGERED":
            out.add(KIND_CLASS.get(r["kind"]) or ("deadline" if r["kind"] in TERM_DEADLINES and r["due"] >= D else "other"))
    return sorted(out, key=CLASSES.index) or ["none"]


def label(r: dict) -> str:
    if r["status"] == "NEEDS_LAWYER":
        return "NEEDS_LAWYER"
    if r["status"] != "TRIGGERED":
        return "none"
    return r["kind"].replace("_", " ") + (f" by {r['due']}" if r["due"] else "")


def cells(rows: list[dict]) -> dict:
    by = {}
    for r in rows:
        by.setdefault(r["scenario"], {}).setdefault(r["contract"], []).append(r)
    return {sc: {k: dict(classes=cell_classes(rs), labels=sorted({label(r) for r in rs}), pending=any(r["unconfirmed"] for r in rs),
                         reactions=[{x: r[x] for x in ("template", "status", "kind", "holder", "due", "reason")} for r in rs])
                 for k, rs in sorted(cs.items())} for sc, cs in by.items()}


def summary(sc_id: str, cs: dict, questions: list, rows: list) -> dict:
    """Header numbers for one scenario: contracts that react, per class, plus open questions and pending outcomes."""
    n = Counter(c for cell in cs.values() for c in set(cell["classes"]))
    pend = {(r["contract"], r["template"]) for r in rows if r["scenario"] == sc_id and r["unconfirmed"]}
    return dict(react=sum(1 for cell in cs.values() if cell["classes"] != ["none"]), consent=n["consent"], notice=n["notice"],
                termination=n["termination"], lawyer=n["lawyer"], prepayment=n["prepayment"], deadline=n["deadline"],
                open_questions=sum(1 for q in questions if sc_id in q["pairs"]), pending=len(pend))


def who(holder: str, party: str) -> str:
    return {"us": f"our party ({party})", "them": "counterparty", "either": "either party", "both": "both parties"}.get(holder, "-")


# ---------------- review queue ----------------
def blocking_questions(ctx: Ctx, library: list) -> list[dict]:
    """Ranked blocking questions (unknown atoms that can change a reaction), English and Arabic contracts together."""
    out = []
    for lang, run_name in (("en", "main"), ("ar", "main_ar")):
        keys = [k for k, c in ctx.contracts.items() if c["lang"] == lang]
        atoms = {k: ctx.atoms[k] for k in keys if k in ctx.atoms}
        for q in review.questions(atoms, library, ctx.con, run=run_name):
            cited = q.cited or ctx.cited(q.contract, q.template, q.atom)
            out.append(dict(id=q.id, contract=q.contract, template=q.template, atom=q.atom, text=q.text, allowed=q.allowed,
                            cited=[[sid, t] for sid, t in cited], pairs=q.pairs, firm_pairs=q.firm_pairs,
                            preview=[dict(answer=review.short(p["answer"]),
                                          changes=[f"{c['scenario']}: {c['before']} → {c['after']}" for c in p["changes"]])
                                     for p in q.preview]))
    out.sort(key=lambda x: (-x["firm_pairs"], -len(x["pairs"]), x["contract"], x["template"], x["atom"]))
    return out


def path_cards(rows: list[dict], ctx: Ctx, info: dict) -> list[dict]:
    """One reasoning-path card per pending outcome: every unconfirmed load-bearing atom of it, to confirm together.
    Outcomes of one clause that rest on the same atoms share a card. Rank: weakest compile evidence, then most outcomes."""
    cards = {}
    for r in rows:
        if not r["unconfirmed"]:
            continue
        c = cards.setdefault((r["contract"], r["template"], tuple(r["unconfirmed"])), dict(outcomes={}))
        c["outcomes"].setdefault(r["scenario"], set()).add(label(r))
    out = []
    for (key, t, atoms), c in cards.items():
        weakest = min((info[f"{key}|{t}|{a}"]["agree"], info[f"{key}|{t}|{a}"]["support"]) for a in atoms)
        out.append(dict(contract=key, template=t, atoms=list(atoms), weakest=list(weakest),
                        outcomes=[dict(scenario=s, label="; ".join(sorted(ls))) for s, ls in sorted(c["outcomes"].items())]))
    out.sort(key=lambda c: (c["weakest"][0] >= 3 and c["weakest"][1], c["weakest"][0], -len(c["outcomes"]), c["contract"], c["template"]))
    return out


def card_counts(cards: list[dict]) -> dict:
    """Reviewer effort on a start: cards vs single-atom confirmations. Opening a card confirms all its atoms, so a later card
    whose atoms are all confirmed already is never opened."""
    seen, opened = set(), 0
    for c in cards:
        atoms = {(c["contract"], c["template"], a) for a in c["atoms"]}
        if atoms - seen:
            opened += 1
            seen |= atoms
    return dict(pending_outcomes=sum(len(c["outcomes"]) for c in cards), cards=len(cards), cards_opened=opened,
                single_atom_confirmations=len(seen), clauses=len({(c["contract"], c["template"]) for c in cards}))


# ---------------- exports ----------------
def build(ctx: Ctx, library=LIBRARY) -> dict:
    """Everything the exports and the page need, from one deterministic run."""
    rows = run(library, ctx)
    cs = cells(rows)
    info = {f"{r['contract']}|{r['template']}|{a}": None for r in rows for a in r["atoms"]}
    info = {k: ctx.atom_info(*k.split("|")) for k in sorted(info)}
    qs = blocking_questions(ctx, library)
    cards = path_cards(rows, ctx, info)
    sents = {}
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
    return dict(as_of=D, atoms="gold" if ctx.gold else "compiled + reviews", reviews=len(ctx.reviews),
                scenarios=[dict(id=sc["id"], name=sc["name"], templates=sc["templates"], events=sc["events"],
                                summary=summary(sc["id"], cs.get(sc["id"], {}), qs, rows)) for sc in library],
                contracts={k: dict(title=c["title"], party=c["party"], lang=c["lang"]) for k, c in sorted(ctx.contracts.items())},
                cells=cs, rows=rows, atominfo=info, sentences=sents, questions=qs, cards=cards, effort=card_counts(cards))


def schedule_csv(data: dict, sc_id: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["contract", "title", "our_party", "template", "reaction", "who_acts", "due", "unconfirmed_atoms", "reason", "clause"])
    for r in data["rows"]:
        if r["scenario"] != sc_id or r["status"] != "TRIGGERED":
            continue
        c = data["contracts"][r["contract"]]
        clause = " … ".join(f"[{sid}] {data['sentences'][r['contract']][str(sid)]}" for sid in r["sids"])
        w.writerow([r["contract"], c["title"], c["party"], r["template"], r["kind"], who(r["holder"], c["party"]),
                    r["due"] or "no date in the clause", " ".join(r["unconfirmed"]), r["reason"], clause])
    return buf.getvalue()


def write(data: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for sc in data["scenarios"]:
        (out / f"schedule_{sc['id']}.csv").write_bytes(schedule_csv(data, sc["id"]).encode("utf-8-sig"))
    exposure = dict(as_of=data["as_of"], atoms=data["atoms"], scenarios=[{k: s[k] for k in ("id", "name", "events")} for s in data["scenarios"]],
                    contracts=data["contracts"], cells=data["cells"])
    (out / "exposure.json").write_bytes(json.dumps(exposure, indent=1, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    page = (Path(__file__).parent / "portfolio.html").read_text(encoding="utf-8")
    slim = {**data, "cells": {sc: {k: {x: v[x] for x in ("classes", "labels", "pending")} for k, v in cs.items()}
                              for sc, cs in data["cells"].items()}}       # the page reads reactions from the rows
    blob = json.dumps(slim, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    (out / "index.html").write_bytes(page.replace("/*DATA*/null", blob).encode("utf-8"))


# ---------------- checks ----------------
def verbatim_errors(data: dict, ctx: Ctx) -> list[str]:
    """Every TRIGGERED reaction cites at least one sentence, and each cited sentence is the exact text at its offsets in the
    source file."""
    errs = []
    src = {k: (ROOT / c["path"]).read_bytes().decode("utf-8") for k, c in ctx.contracts.items()}
    spans = {(cid, sid): (a, b) for cid, sid, a, b in ctx.con.execute("SELECT contract_id, sid, char_start, char_end FROM sentence")}
    for r in data["rows"]:
        if r["status"] != "TRIGGERED":
            continue
        if not r["sids"]:
            errs.append(f"{r['scenario']} {r['contract']} {r['template']}: no cited sentence")
        for sid in r["sids"]:
            text = data["sentences"][r["contract"]][str(sid)]
            a, b = spans[(ctx.contracts[r["contract"]]["cid"], sid)]
            if src[r["contract"]][a:b] != text:
                errs.append(f"{r['contract']} [{sid}] not verbatim")
    return errs


def check(out: Path) -> None:
    import tempfile
    assert cell_classes([dict(status="TRIGGERED", kind="consent", due=""), dict(status="NEEDS_LAWYER", kind="", due="")]) == ["lawyer", "consent"]
    assert cell_classes([dict(status="TRIGGERED", kind="renewed", due="")]) == ["other"]
    assert cell_classes([dict(status="TRIGGERED", kind="deadline", due="2026-12-01")]) == ["deadline"]
    assert cell_classes([dict(status="NOT_TRIGGERED", kind="", due="")]) == ["none"]
    cards = [dict(contract="c", template="coc", atoms=["a", "b"], outcomes=[1, 2]), dict(contract="c", template="coc", atoms=["a"], outcomes=[3])]
    assert card_counts(cards) == dict(pending_outcomes=3, cards=2, cards_opened=1, single_atom_confirmations=2, clauses=1)
    ctx = Ctx()
    data = build(ctx)
    errs = verbatim_errors(data, ctx)
    assert not errs, errs[:5]
    trig = sum(r["status"] == "TRIGGERED" for r in data["rows"])
    with tempfile.TemporaryDirectory() as tmp:
        write(build(Ctx()), Path(tmp))
        write(data, out)
        names = sorted(p.name for p in Path(tmp).iterdir() if p.suffix in (".csv", ".json"))
        same = [n for n in names if (Path(tmp) / n).read_bytes() == (out / n).read_bytes()]
        assert same == names, sorted(set(names) - set(same))
    print(f"portfolio check ok: {trig} triggered reactions cite verbatim text; {len(names)} exports byte-identical across two runs")


# ---------------- reviewer effort, gold atoms as the lawyer ----------------
def measure() -> dict:
    """Walk the scenario cases from the compiled start like the review oracle, but confirm load-bearing atoms one
    reasoning-path card at a time: the card holds every unconfirmed load-bearing atom of one pending outcome.
    Blocking questions are still answered one by one. Compare with the atom-at-a-time walk."""
    from tests import review_oracle as ro
    from tests import score_cases as scs
    start = ro.start_atoms("compiled")
    scenarios = review.case_scenarios(scs.cases)
    con = sqlite3.connect(DB)
    reviews, skipped, n, worst = [], set(), Counter(), 0

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
        qs = review.questions(atoms, scenarios, con, reviews=reviews)
        pend = {p for q in qs if q.kind == "confirm" for p in q.pairs}
        worst = max(worst, ro.score(atoms, pend)[0]["wrong firm"])
        todo = [q for q in qs if q.id not in skipped]
        if not todo:
            break
        q = todo[0]
        fam = atoms.get(q.contract, {}).get(q.template, {})
        if q.kind == "blocking":
            j = ro.judge(q, fam)
            if j is None:
                skipped.add(q.id)
            else:
                store(q, *j)
            continue
        sc = next(x for x in scenarios if x["id"] == q.pairs[0])
        done = review.confirmed(reviews)
        card = [a for a in review.load_bearing(q.template, fam, state_of(sc)) if (q.contract, q.template, a) not in done]
        judged = 0
        for x in qs:
            if x.kind == "confirm" and x.contract == q.contract and x.template == q.template and x.atom in card:
                j = ro.judge(x, fam)
                if j is None:
                    skipped.add(x.id)
                else:
                    store(x, *j)
                    judged += 1
        if judged:
            n["cards"] += 1
    atoms = review.apply_reviews(start, reviews)
    pend = {p for q in review.questions(atoms, scenarios, con, reviews=reviews) if q.kind == "confirm" for p in q.pairs}
    tally = ro.score(atoms, pend)[0]
    atomwise = ro.experiment("compiled")
    return dict(cases=len(scs.cases), card_walk=dict(answers=n["answer"], cards=n["cards"], atoms_confirmed=n["confirm"],
                                                     atoms_corrected=n["correct"], end=tally, max_wrong_firm=worst),
                atom_walk=dict(totals=atomwise["totals"], max_correct=atomwise["max_correct"], max_wrong_firm=atomwise["max_wrong_firm"]))


def main():
    ap = argparse.ArgumentParser(prog="python -m twin.portfolio")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "check", "measure"])
    ap.add_argument("--out", default="out/portfolio/")
    ap.add_argument("--atoms", default="compiled", choices=["compiled", "gold"])
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    out = ROOT / a.out
    if a.cmd == "check":
        return check(out)
    if a.cmd == "measure":
        ctx = Ctx()
        res = dict(portfolio_start=build(ctx)["effort"], scenario_cases=measure())
        out.mkdir(parents=True, exist_ok=True)
        (out / "effort.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(json.dumps(res, indent=1))
        return
    data = build(Ctx(a.atoms))
    write(data, out)
    for sc in data["scenarios"]:
        s = sc["summary"]
        print(f"{sc['id']:14} {s['react']:2} contracts react: {s['consent']} consents, {s['notice']} notices, "
              f"{s['termination']} termination rights, {s['lawyer']} for a lawyer; open questions {s['open_questions']}, pending {s['pending']}")
    print("effort:", data["effort"])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
