"""Review queue: every NEEDS_LAWYER becomes a closed question about ONE atom of ONE contract.

    python -m twin.review queue [--all] [--no-confirm]  # ranked questions (scenario cases, or every case scenario x every contract)
    python -m twin.review show <id>                     # one question as Markdown
    python -m twin.review answer <id> <value> --by NAME [--reason TEXT]    # for a confirm question, <value> "confirm" keeps the compiled value

Questions come from the rules, not from a model: an atom is asked only when some value of it can change a reaction.
Two kinds: blocking questions (the atom is unknown) and confirm questions (the atom is known, but some other value would change
a reaction that is otherwise firm). An outcome counts as firm only once its load-bearing atoms are confirmed or corrected.
An answer is stored in the `review` table as a review of the atom and overrides the compiled atom in later runs.
No model is called here.
"""
import argparse
import copy
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product

from . import compile as comp
from .core import State, apply
from .ingest import DB, ROOT
from .rules import CHOICE, MAX_READINGS, POWER, TEMPLATES, _critical, react

NL = "NEEDS_LAWYER"
GROUP = {"initial_term_n": "initial_term", "initial_term_unit": "initial_term", "notice_n": "notice", "notice_unit": "notice",
         "renewal_term_n": "renewal_term", "renewal_term_unit": "renewal_term"}
BOOLS = {"coc_not_assignment", "merger_exception", "unless_competitor"}
TRUE_FALSE = BOOLS | {"post_ipo_relative_to_retained"}   # yes/no atoms of either template
EFFECT_CANDIDATES = [[{"effect": e, "effect_holder": h}] for e in comp.EFFECTS for h in ("us", "them", "either")]


# ---------------- atoms of a gold-shaped family ----------------
def atom_of(blocker: str) -> str:
    """Name the atom a blocker string ('whose is unknown') is about. Unit and number of one period are one atom."""
    if blocker.startswith("silence guard"):
        return "present"
    key = blocker.split(" is ")[0]
    if key == "unless_consented" and " for " in blocker:
        return "unless_consented:" + blocker.rsplit(" for ", 1)[1]
    return GROUP.get(key, key)


def blocker_atoms(fam: dict) -> list[str]:
    out = []
    for b in fam.get("_unknown", []):
        if atom_of(b) not in out:
            out.append(atom_of(b))
    return out


def get_effects(fam: dict) -> list[dict]:
    if "effects" in fam:
        return copy.deepcopy(fam["effects"])
    if "effect" in fam:
        return [{k: fam[k] for k in ("effect", "effect_holder", "notice", "unless_consented") if k in fam and fam[k] is not None}]
    return []


def put_effects(fam: dict, effs: list[dict]) -> None:
    """Single plain effect -> flat fields; otherwise the list form (as the compiler writes them)."""
    for k in ("effect", "effect_holder", "notice", "unless_consented", "effects"):
        fam.pop(k, None)
    if len(effs) == 1 and not effs[0].get("unless_consented"):
        fam.update({k: v for k, v in effs[0].items() if v is not None})
    elif effs:
        fam["effects"] = effs


def set_atom(fam: dict, atom: str, value) -> None:
    if atom == "effects":
        put_effects(fam, copy.deepcopy(value))
    elif atom.startswith("unless_consented:"):
        effs = get_effects(fam)
        for e in effs:
            if e["effect"] == atom.split(":", 1)[1]:
                e["unless_consented"] = bool(value)
                if not value:
                    e.pop("unless_consented")
        put_effects(fam, effs)
    elif atom == "qipo_threshold":
        fam.setdefault("qualifying_ipo", {})["threshold"] = value
    elif atom != "present":
        fam[atom] = copy.deepcopy(value)


def missing(template: str, fam: dict) -> list[str]:
    """Atoms the rules need that this family does not have yet, given the values it does have."""
    if template == "coc":
        if fam.get("whose") == "them":
            return []                       # the clause is about the other party: nothing else can matter
        need = ["whose", "triggers"] + ([] if "effect" in fam or "effects" in fam else ["effects"])
        t = fam.get("triggers") or []
        if "ownership_threshold" in t:
            need += ["threshold_pct", "threshold_op"]
        if "retained_stake" in t:
            need += ["retained_holder", "retained_pct"]
        if "competitor_stake" in t:
            need += ["competitor_threshold_pct"]
        if "transaction_type" in t:
            need += ["transaction_types"]
        if "open_textured" in t:
            need += ["open_texture"]
        if fam.get("_fresh"):                   # a clause nobody has read yet: the optional exceptions must be asked too
            need += ["carve_outs"]
        if fam.get("_fresh") and "retained_stake" in t:
            need += ["qualifying_ipo"]
        q = fam.get("qualifying_ipo")
        if q:
            need += ["post_ipo_threshold_pct"] + (["qipo_threshold"] if q.get("test") == "proceeds" and "threshold" not in q else [])
            if fam.get("_fresh"):
                need += ["post_ipo_relative_to_retained"]
    elif template == "assign":
        need = ["restricted"] + (["consent_needed"] if fam.get("restricted") is True else [])
        if fam.get("_fresh"):
            need += ["deems", *sorted(BOOLS)]
    else:
        need = ["mode"]
        if fam.get("mode") != "conditional" and "end_date" not in fam:
            need += ["start", "initial_term"]
    return [a for a in need if a not in fam and not (a == "effects" and get_effects(fam))
            and not (a == "qipo_threshold" and "threshold" in fam.get("qualifying_ipo", {}))]


# ---------------- reviews ----------------
def apply_reviews(atoms: dict, reviews: list[dict]) -> dict:
    """Atoms (contract -> template -> family) with the reviews laid over them; later reviews win. The families a review touches
    get their blocker list rebuilt: answered atoms and atoms that no longer matter drop out, newly needed atoms come in."""
    out = copy.deepcopy(atoms)
    touched = {}
    for r in reviews:
        fam = out.get(r["contract_id"], {}).get(r["template"])
        if fam is None or r.get("kind") == "confirm":       # a confirmation keeps the compiled value
            continue
        if r["atom"] == "present" and r["value"] == "no":
            del out[r["contract_id"]][r["template"]]
            continue
        set_atom(fam, r["atom"], r["value"])
        if r["atom"] == "present":
            fam["_fresh"] = True
        touched.setdefault((r["contract_id"], r["template"]), set()).add(r["atom"])
    for (cid, t), done in touched.items():
        fam = out.get(cid, {}).get(t)
        if fam is None:
            continue
        need = missing(t, fam)
        old = [b for b in fam.get("_unknown", []) if atom_of(b) not in done and (atom_of(b) in need or (
            atom_of(b).startswith("unless_consented:") and fam.get("whose") != "them"))]
        old += [f"{a} is unknown" for a in need if a not in {atom_of(b) for b in old}]
        fam.pop("_unknown", None)
        if old:
            fam["_unknown"] = old
    return out


def load_reviews(con) -> list[dict]:
    """All reviews, oldest first. `kind` is answer (a blocking question), confirm (compiled value kept) or correct (value replaced)."""
    con.execute("CREATE TABLE IF NOT EXISTS review(contract_id TEXT, template TEXT, atom TEXT, value TEXT, reviewer TEXT, "
                "reason TEXT, scope TEXT, at TEXT, kind TEXT DEFAULT 'answer')")
    if "kind" not in {r[1] for r in con.execute("PRAGMA table_info(review)")}:
        con.execute("ALTER TABLE review ADD COLUMN kind TEXT DEFAULT 'answer'")
    return [dict(contract_id=c, template=t, atom=a, value=json.loads(v), reviewer=r, reason=why, scope=s, at=at, kind=k or "answer")
            for c, t, a, v, r, why, s, at, k in con.execute(
                "SELECT contract_id, template, atom, value, reviewer, reason, scope, at, kind FROM review ORDER BY rowid")]


def add_review(con, contract_id, template, atom, value, reviewer, reason="", scope="contract", kind="answer"):
    load_reviews(con)
    con.execute("INSERT INTO review(contract_id, template, atom, value, reviewer, reason, scope, at, kind) VALUES(?,?,?,?,?,?,?,?,?)",
                (contract_id, template, atom, json.dumps(value), reviewer, reason, scope,
                 datetime.now(timezone.utc).isoformat(timespec="seconds"), kind))
    con.commit()


def confirmed(reviews: list[dict]) -> set:
    """(contract, template, atom) a lawyer has looked at: answered, confirmed or corrected. A given end date settles the term atoms too."""
    out = set()
    for r in reviews:
        out.add((r["contract_id"], r["template"], r["atom"]))
        if r["atom"] == "end_date":
            out |= {(r["contract_id"], r["template"], "start"), (r["contract_id"], r["template"], "initial_term")}
    return out


def expand(atom: str, value) -> list[tuple]:
    """An answer as (atom, value) pairs to store. A term can also be answered with {"end_date": "YYYY-MM-DD"}."""
    if atom in ("start", "initial_term") and isinstance(value, dict) and set(value) == {"end_date"}:
        return [("end_date", value["end_date"])]
    return [(atom, value)]


# ---------------- what could an answer change? ----------------
def candidates(template: str, atom: str, s: State):
    """Values worth trying for an atom in this state, or None when the value is free (then any value may matter)."""
    if atom == "present":
        return ["yes", "no"]
    if atom == "effects":
        return EFFECT_CANDIDATES
    if atom.startswith("unless_consented:"):
        return [True, False]
    if atom in CHOICE:
        return CHOICE[atom]
    if atom in TRUE_FALSE:
        return [True, False]
    if atom == "qualifying_ipo":
        return [False, {"test": "listing"}, {"test": "proceeds"}]
    if atom == "deems":
        from .rules import _subsets
        return _subsets(comp.ASSIGN["deems"][1], True)
    if atom in POWER:
        from .rules import _subsets
        return _subsets(POWER[atom], atom == "carve_outs")
    c = _critical(atom, s)
    return c or None


def outcome(template: str, fam: dict | None, s: State):
    if fam is None:
        return ("ABSENT",)
    if missing(template, fam):
        return (NL,)
    try:
        rs = TEMPLATES[template]({k: v for k, v in fam.items() if not k.startswith("_")}, s)
    except (KeyError, TypeError, ValueError):
        return (NL,)
    return tuple(sorted((r.status, r.kind, r.holder, str(r.due)) for r in rs))


def relevant_atoms(template: str, fam: dict, s: State) -> list[str]:
    """Blocking atoms with some value that changes the outcome, the other blockers being resolved in every possible way.
    Atoms with a free value (numbers, dates) cannot be tried, so all blockers count when one of those is present."""
    atoms = blocker_atoms(fam)
    doms = [candidates(template, a, s) for a in atoms]
    n = 1
    for d in doms:
        n *= len(d) if d else 1
    if any(d is None for d in doms) or n > MAX_READINGS:
        return atoms
    seen = [{} for _ in atoms]                  # per atom: the other values -> outcomes seen
    for idx in product(*[range(len(d)) for d in doms]):
        f = copy.deepcopy(fam)
        f.pop("_unknown", None)
        gone = False
        for a, d, i in zip(atoms, doms, idx):
            if a == "present" and d[i] == "no":
                gone = True
            set_atom(f, a, d[i])
        o = outcome(template, None if gone else f, s)
        for j in range(len(atoms)):
            seen[j].setdefault(idx[:j] + idx[j + 1:], set()).add(o)
    rel = [a for a, m in zip(atoms, seen) if any(len(v) > 1 for v in m.values())]
    return rel or atoms         # the rules still say NEEDS_LAWYER: some blocker must be resolved, so none can be skipped


def label(rs) -> str:
    return "; ".join(sorted({r.status + (f" {r.kind}" if r.kind and r.status == "TRIGGERED" else "")
                             + (f" ({r.holder})" if r.holder and r.status == "TRIGGERED" else "") for r in rs}))


def short(answer) -> str:
    """An answer as one short phrase."""
    if isinstance(answer, list) and answer and isinstance(answer[0], dict):
        return "; ".join(f"{e['effect']}/{e.get('effect_holder', '?')}" for e in answer)
    return json.dumps(answer)


def firm(rs) -> bool:
    return all(r.status != NL for r in rs)


def run_pair(atoms, gk, template, s):
    fam = atoms.get(gk, {}).get(template)
    return None if fam is None else react(template, fam, s)


# ---------------- questions ----------------
@dataclass
class Question:
    id: str
    contract: str
    template: str
    atom: str
    text: str
    allowed: str
    cited: list
    pairs: list
    firm_pairs: int = 0
    preview: list = field(default_factory=list)
    kind: str = "blocking"          # blocking: the atom is unknown. confirm: the atom is known and load-bearing for a firm outcome
    current: object = None          # confirm only: the compiled value
    agreement: tuple = (0, False)   # confirm only: (fewest compile runs that agreed on the atom, support check passed on all)

    def markdown(self) -> str:
        lines = [f"### {self.contract} — {self.template} — `{self.atom}`", ""]
        if self.kind == "confirm":
            lines += [f"**Confirm: compiled value = {short(self.current)}** (compile agreement {self.agreement[0]}/3, "
                      f"support check {'passed' if self.agreement[1] else 'not passed'}). Answer `confirm`, or give the right value.", ""]
        lines += [f"**Question.** {self.text}", "",
                 f"**Allowed answers.** {self.allowed}", "", "**Cited sentences (verbatim).**", ""]
        lines += [f"> [{sid}] {t}" for sid, t in self.cited] or ["> (no sentence cited)"]
        lines += ["", f"**Affects** {len(self.pairs)} scenario(s): {', '.join(self.pairs)}; "
                  + (f"they stay pending confirmation until this is confirmed or corrected." if self.kind == "confirm" else
                     f"{self.firm_pairs} of them become firm with one answer."), "", "**Impact preview (reactions that change).**", ""]
        groups = {}
        for p in self.preview:
            ch = "; ".join(f"{c['scenario']}: {c['before']} → {c['after']}" for c in p["changes"]) or "no change"
            groups.setdefault(ch, []).append(short(p["answer"]))
        for ch, answers in groups.items():
            shown = ", ".join(f"`{a}`" for a in answers[:4]) + (f" and {len(answers) - 4} more" if len(answers) > 4 else "")
            lines.append(f"- {shown}: {ch}")
        return "\n".join(lines) + "\n"


def question_text(template: str, atom: str) -> tuple[str, str]:
    """(question, allowed answers) from the atom definitions the compiler used."""
    defs = comp.ATOMS[template]
    if atom == "present":
        return ("Does the contract contain this kind of provision at all?" if template != "assign" else defs["present"][2],
                '"yes" or "no" (no removes the clause from the rules)')
    if atom == "effects":
        return (defs["effects"][2], "a list of effects: " + ", ".join(comp.EFFECTS) +
                "; each with effect_holder (us/them/either), optional notice [n, days|months|business_days], optional unless_consented")
    if atom.startswith("unless_consented:"):
        return (f"Does prior written consent of the other party remove the '{atom.split(':', 1)[1]}' effect (and only that one)?",
                "true or false")
    if atom == "qualifying_ipo":
        return ("Does a change of control exception apply after a Qualifying IPO (a listing, possibly with minimum proceeds) that replaces the retained-stake test?",
                'false (no such exception), {"test": "listing"}, or {"test": "proceeds", "threshold": number}')
    if atom == "post_ipo_relative_to_retained":
        return ("After a Qualifying IPO, must the largest other holder also hold at least as much as the retained holder?", "true or false")
    if atom == "start":
        return (defs["start"][2], 'a date YYYY-MM-DD, or {"end_date": "YYYY-MM-DD"} when the contract states an end date and no start')
    keys = [k for k in defs if GROUP.get(k, k) == atom] or [atom]
    kind, vals, q = defs.get(keys[0], ("num", None, f"What is the value of {atom}?"))
    if atom == "initial_term":
        return (" ".join(defs[k][2] for k in keys), '[number, unit] with unit days, months, years, business_days, or "redacted"; '
                'or {"end_date": "YYYY-MM-DD"} when the contract states an end date instead of a length')
    if len(keys) > 1:
        return (" ".join(defs[k][2] for k in keys), "[number, unit] with unit days, months, years, business_days, or \"redacted\"")
    if kind == "enum":
        return (q, " or ".join(json.dumps(v) for v in vals))
    if kind == "set":
        return (q, "any list drawn from: " + ", ".join(vals))
    if atom in ("restricted", "consent_needed") or atom in TRUE_FALSE:
        return (q, "true or false")
    if atom == "mode":
        return (q, " or ".join(json.dumps(v) for v in vals))
    return (q, "a number" if kind == "num" or "pct" in atom or "threshold" in atom else "a date YYYY-MM-DD" if atom in ("start", "end_date") else "a value")


# The question a lawyer is asked on the page for each atom: plain words, answered from the clause. It only rewords the
# definition above; the compiler prompt and the allowed answers are unchanged.
ASK = {
    ("coc", "present"): "Does the contract have a clause that reacts to a change of control or a change in ownership?",
    ("coc", "whose"): "Does the clause cover a change of control of our side, of the other side, or of either?",
    ("coc", "triggers"): "Which events does the clause treat as a change of control (for example a share sale, a merger, a change in management)?",
    ("coc", "threshold_pct"): "What share of ownership, bought by one buyer, counts as a change of control?",
    ("coc", "threshold_op"): "Does the clause say \"more than\" that share, or \"that share or more\"?",
    ("coc", "competitor_threshold_pct"): "What share must a competitor buy before the clause applies?",
    ("coc", "retained_holder"): "Does the clause test what a named group (the sponsors, the original owners, the principal owners) keeps, or is there no such test?",
    ("coc", "retained_pct"): "What share must that group keep so that the clause does not apply?",
    ("coc", "transaction_types"): "Which kinds of deal count (merger, consolidation, asset sale, reorganization, share sale)?",
    ("coc", "qualifying_ipo"): "Is there an exception after a qualifying IPO (a listing, possibly with a minimum raise)? If so, what is the test?",
    ("coc", "post_ipo_threshold_pct"): "After a qualifying IPO, what share held by one person counts as a change of control?",
    ("coc", "post_ipo_relative_to_retained"): "After a qualifying IPO, must that person also hold at least as much as the group that kept its stake?",
    ("coc", "carve_outs"): "Which exceptions does the clause give (same management stays, a qualifying IPO, prior holders keep 50%, consent was given, the buyer is not a competitor)?",
    ("coc", "open_texture"): "Does the clause rely on the other party's judgement, or on a size word such as \"significant\" or \"material\"?",
    ("coc", "effects"): "What happens on a change of control, who may use it, and within how long must notice be given?",
    ("coc", "definition_location"): "Where is the definition of change of control: in this clause, elsewhere in the contract, or in another document?",
    ("assign", "present"): "Does the contract restrict assigning or transferring the agreement?",
    ("assign", "restricted"): "Is assignment restricted?",
    ("assign", "consent_needed"): "Must the other party consent before the agreement is assigned?",
    ("assign", "deems"): "Which events does the clause treat as an assignment (for example a merger or a change of control)?",
    ("assign", "coc_not_assignment"): "Does the contract say that a change of control is not an assignment?",
    ("assign", "merger_exception"): "May a party assign without consent in a merger or a sale of the business?",
    ("assign", "unless_competitor"): "Is that freedom lost if the successor is a competitor?",
    ("term", "present"): "Does the contract state a term, an end date or a way to renew?",
    ("term", "start"): "On what date does the term start (or does it start on signing)?",
    ("term", "end_date"): "What fixed end date does the text state?",
    ("term", "initial_term"): "How long is the first term?",
    ("term", "mode"): "After the first term, does the contract renew by itself, run until someone gives notice, let one party extend, need both parties to agree, depend on other facts, or stop?",
    ("term", "option_holder"): "Who may extend the contract: us, them, or either?",
    ("term", "renewal_term"): "How long is each renewal?",
    ("term", "notice"): "How much notice must be given to stop a renewal or to leave?",
}


def ask_text(template: str, atom: str) -> str:
    """The plain question for one atom; for an atom without an entry, the compiler's own wording."""
    if atom.startswith("unless_consented:"):
        return f"Does the other party's written consent remove the \"{atom.split(':', 1)[1].replace('_', ' ')}\" effect, and only that one?"
    return ASK.get((template, atom)) or question_text(template, atom)[0]


def atom_keys(template: str, atom: str) -> list[str]:
    """The compiler's atom keys behind one reviewed atom."""
    if atom == "qualifying_ipo":
        return ["qipo_test", "qipo_threshold"]
    return [k for k in comp.ATOMS[template] if GROUP.get(k, k) == atom] or [atom.split(":")[0]]


def cited_text(con, cid: str, template: str, atom: str, fam: dict, run: str = "main"):
    """Sentences the compiler cited for the atom; else those it cited for the rest of the clause; else the best keyword hits."""
    keys = atom_keys(template, atom)
    def sids(ks):
        out = set()
        for (s,) in con.execute(f"SELECT sids FROM atom WHERE run=? AND contract_id=? AND template=? AND key IN ({','.join('?' * len(ks))})",
                                [run, cid, template, *ks]):
            out |= set(json.loads(s))
        return out
    chosen = sids(keys) or sids([k for k in comp.ATOMS[template] if k != "present"])
    text = dict(con.execute("SELECT sid, text FROM sentence WHERE contract_id=?", (cid,)))
    if not chosen:
        chosen = {sid for sid, _ in comp.retrieve(con, cid, template)[:3]}
    return [(sid, text[sid]) for sid in sorted(chosen)[:6] if sid in text]


def preview_values(template: str, atom: str, states: list) -> list:
    if atom in CHOICE or atom in TRUE_FALSE or atom in ("present", "effects", "qualifying_ipo", "deems") or atom.startswith("unless_consented:"):
        return candidates(template, atom, states[0])
    if atom in POWER:
        return [[v] for v in POWER[atom]] + ([[]] if atom == "carve_outs" else [])
    return sorted({v for s in states for v in (_critical(atom, s) or [])})


def contract_key(cid: str, lang: str) -> str:
    """Key of a contract in the compiled-atom files: Arabic contracts use their own `ar:` key, not the English source's."""
    return comp.gold_key(cid) if lang == "en" else comp.PORTFOLIO[cid]["src"]


def questions(atoms_by_contract: dict, scenarios: list[dict], con=None, run: str = "main", reviews=None) -> list[Question]:
    """Ranked, de-duplicated questions. A scenario is {id, events, templates?, only?}: `only` limits it to one contract.
    Rank: reactions one answer makes firm, then reactions the atom can change, then contract name.
    With `reviews` given (the list already laid over the atoms), confirm questions follow the blocking ones."""
    con = con or sqlite3.connect(DB)
    cids = {contract_key(c, lang): c for c, lang in con.execute("SELECT id, language FROM contract")}
    qs: dict[tuple, dict] = {}
    for sc in scenarios:
        s = State()
        for ev in sc["events"]:
            s = apply(s, ev)
        templates = sc.get("templates") or (["term"] if all(e["event"] == "date_advance" for e in sc["events"]) else ["coc", "assign"])
        for gk in sorted([sc["only"]] if sc.get("only") else atoms_by_contract):
            for t in templates:
                fam = atoms_by_contract.get(gk, {}).get(t)
                if not fam or not fam.get("_unknown"):
                    continue
                base = react(t, fam, s)
                if firm(base):
                    continue
                for a in relevant_atoms(t, fam, s):
                    q = qs.setdefault((gk, t, a), dict(pairs=[], states=[], before=[], fam=fam))
                    q["pairs"].append(sc["id"])
                    q["states"].append(s)
                    q["before"].append(base)
    out = []
    for (gk, t, a), q in qs.items():
        fam, text_q, allowed = q["fam"], *question_text(t, a)
        prev, firm_ids = [], set()
        for v in preview_values(t, a, q["states"]):
            after = apply_reviews({gk: {t: fam}}, [dict(contract_id=gk, template=t, atom=a, value=v)])
            changes = []
            for sid, s, before in zip(q["pairs"], q["states"], q["before"]):
                rs = run_pair(after, gk, t, s) or []
                if rs and firm(rs):
                    firm_ids.add(sid)
                if label(rs) != label(before):
                    changes.append(dict(scenario=sid, before=label(before), after=label(rs) or "clause removed"))
            prev.append(dict(answer=v, changes=changes))
        if not prev:        # a free value (a date or a term length): it cannot be tried, so say what depends on it
            prev = [dict(answer="(the value given)", changes=[dict(scenario=sid, before=label(b), after="firm once the value is known")
                                                              for sid, b in zip(q["pairs"], q["before"])])]
        out.append(Question(f"{gk}|{t}|{a}", gk, t, a, text_q, allowed,
                            cited_text(con, cids[gk], t, a, fam, run + "_ar" if gk.startswith("ar:") else run) if gk in cids else [], q["pairs"], len(firm_ids), prev))
    out.sort(key=lambda x: (-x.firm_pairs, -len(x.pairs), x.contract, x.template, x.atom))
    if reviews is not None:
        more = confirm_questions(atoms_by_contract, scenarios, reviews, con, run, cids)
        out += [q for q in more if q.kind == "blocking"] + [q for q in more if q.kind == "confirm"]
    return out


def unsure(fam: dict) -> set:
    """Atoms the compiler could not settle but the rules did not need to block on: the rules read them as absent.
    Such an atom is asked as a question to answer, never shown as a value to confirm."""
    return {atom_of(b) for b in fam.get("_optional_unknown", [])}


# ---------------- load-bearing atoms ----------------
LOGICAL = {"coc": ["whose", "triggers", "threshold_pct", "threshold_op", "retained_holder", "retained_pct", "competitor_threshold_pct",
                   "transaction_types", "open_texture", "carve_outs", "effects", "qualifying_ipo", "post_ipo_threshold_pct",
                   "post_ipo_relative_to_retained"],
           "assign": ["restricted", "consent_needed", "deems", *sorted(BOOLS)],
           "term": ["mode", "start", "initial_term", "end_date", "notice", "option_holder", "renewal_term"]}
ALWAYS = {"whose", "mode", "deems", "carve_outs", "triggers", *BOOLS}      # tried even when the family does not list them (absent = none)
EFFECT_KEYS = ["effect", "effect_holder", "notice", "unless_consented", "effects"]
_LB: dict = {}


def get_atom(fam: dict, atom: str):
    """The value of a reviewed atom in a family, in the shape a review stores it (inverse of set_atom)."""
    if atom == "present":
        return "yes"
    if atom == "effects":
        return get_effects(fam)
    if atom in ("carve_outs", "deems", "transaction_types", "triggers"):
        return copy.deepcopy(fam.get(atom, []))
    if atom in TRUE_FALSE:
        return bool(fam.get(atom, False))
    if atom == "qualifying_ipo":
        return copy.deepcopy(fam.get(atom)) or False
    return copy.deepcopy(fam.get(atom))


def logical_atoms(template: str, fam: dict) -> list[str]:
    out = ["present"]
    for a in LOGICAL[template]:
        if a == "effects":
            keep = bool(get_effects(fam))
        elif a == "qualifying_ipo":
            keep = a in fam or "retained_stake" in (fam.get("triggers") or [])
        else:
            keep = a in fam or a in ALWAYS
        if keep:
            out.append(a)
    return out


def _sig(rs) -> frozenset:
    """What a lawyer would see: status, and for a triggered reaction its kind, holder and date."""
    return frozenset((r.status, r.kind, r.holder, str(r.due)) if r.status == "TRIGGERED" else (r.status,) for r in rs)


def _run(template: str, fam: dict | None, s: State) -> frozenset:
    if fam is None:
        return frozenset({("NOT_TRIGGERED",)})          # no clause: nothing happens
    try:
        return _sig(react(template, fam, s))
    except (KeyError, TypeError, ValueError):             # this reading needs a number or date that is not there
        return frozenset({(NL,)})


def variants_of(template: str, fam: dict, atom: str, s: State) -> list:
    """Families that differ from `fam` in one atom: each other allowed value, or, for a free value, the value taken away."""
    if atom == "present":
        return [None]
    vals = candidates(template, atom, s)
    out = []
    for v in vals or [None]:
        f = copy.deepcopy(fam)
        if vals:
            if v == get_atom(fam, atom):
                continue
            set_atom(f, atom, v)
        else:
            for k in (EFFECT_KEYS if atom == "effects" else [atom]):
                f.pop(k, None)
        out.append(f)
    return out


def load_bearing(template: str, fam: dict, s: State) -> list[str]:
    """Known atoms of a firm family such that some other allowed value would change a reaction in state s.
    Closed lists and critical numbers are tried value by value; a free value (a date, a term length) is tried by
    taking it away: the rules then need it or they do not."""
    key = (template, json.dumps(fam, sort_keys=True, default=str), repr(s))
    if key in _LB:
        return _LB[key]
    base = _run(template, fam, s)
    out = []
    if not any(x[0] == NL for x in base):
        out = [a for a in logical_atoms(template, fam) if any(_run(template, f, s) != base for f in variants_of(template, fam, a, s))]
    _LB[key] = out
    return out


def agreement(con, cid: str, template: str, atom: str, run: str) -> tuple:
    """(fewest compile runs that agreed on the atom's keys, support check passed on all of them). Unseen atoms count as 0 / False."""
    ks = atom_keys(template, atom)
    rows = con.execute(f"SELECT k_agree, support_ok FROM atom WHERE run=? AND contract_id=? AND template=? AND key IN ({','.join('?' * len(ks))})",
                       [run, cid, template, *ks]).fetchall()
    return (min(r[0] for r in rows), all(r[1] for r in rows)) if rows else (0, False)


def confirm_questions(atoms_by_contract: dict, scenarios: list[dict], reviews: list[dict], con, run, cids) -> list[Question]:
    """One question per load-bearing atom that nobody has looked at, for families whose reactions are firm in some scenario.
    Rank: weakest compile evidence first (fewer than 3 agreeing runs or an unsupported atom), then most scenarios."""
    done = confirmed(reviews)
    qs: dict[tuple, dict] = {}
    for sc in scenarios:
        s = State()
        for ev in sc["events"]:
            s = apply(s, ev)
        templates = sc.get("templates") or (["term"] if all(e["event"] == "date_advance" for e in sc["events"]) else ["coc", "assign"])
        for gk in sorted([sc["only"]] if sc.get("only") else atoms_by_contract):
            for t in templates:
                fam = atoms_by_contract.get(gk, {}).get(t)
                if not fam:
                    continue
                base = react(t, fam, s)
                if not firm(base):
                    continue
                for a in load_bearing(t, fam, s):
                    if (gk, t, a) in done:
                        continue
                    q = qs.setdefault((gk, t, a), dict(pairs=[], states=[], before=[], fam=fam))
                    q["pairs"].append(sc["id"])
                    q["states"].append(s)
                    q["before"].append(base)
    out = []
    for (gk, t, a), q in qs.items():
        fam, cur = q["fam"], get_atom(q["fam"], a)
        text_q, allowed = question_text(t, a)
        prev = []
        for v in ["no"] if a == "present" else [v for v in preview_values(t, a, q["states"]) if v != cur]:
            after = apply_reviews({gk: {t: fam}}, [dict(contract_id=gk, template=t, atom=a, value=v, kind="correct")])
            changes = []
            for sid, s, before in zip(q["pairs"], q["states"], q["before"]):
                rs = run_pair(after, gk, t, s) or []
                if label(rs) != label(before):
                    changes.append(dict(scenario=sid, before=label(before), after=label(rs) or "clause removed"))
            if changes:
                prev.append(dict(answer=v, changes=changes))
        if not prev:        # a free value cannot be tried: say that any other value changes these reactions
            prev = [dict(answer="(any other value)", changes=[dict(scenario=sid, before=label(b), after="changes or becomes undetermined")
                                                               for sid, b in zip(q["pairs"], q["before"])])]
        r = run + "_ar" if gk.startswith("ar:") else run
        ask = a in unsure(fam)          # the compiled "value" is only the rules' default: ask for it
        out.append(Question(f"{gk}|{t}|{a}", gk, t, a, text_q, allowed,
                            cited_text(con, cids[gk], t, a, fam, r) if gk in cids else [], q["pairs"], 0, prev,
                            "blocking" if ask else "confirm", None if ask else cur,
                            agreement(con, cids[gk], t, a, r) if gk in cids else (0, False)))
    out.sort(key=lambda x: (x.agreement[0] >= 3 and x.agreement[1], x.agreement[0], -len(x.pairs), x.contract, x.template, x.atom))
    return out


# ---------------- cases as scenarios ----------------
def case_scenarios(cases: list[dict]) -> list[dict]:
    """The scenario cases as scenarios, each limited to its own contract (same source rule as the case scorer)."""
    out = []
    for c in cases:
        src = c["src"].split(" ")[0]
        src = "edgar:Amscan" if src.startswith("edgar:Amscan") else src
        out.append(dict(id=c["id"], events=c["scenario"], only=src, templates=["term"] if c["id"].startswith("N") else ["coc", "assign"]))
    return out


def load_inputs(everything=False, atoms_file="out/compiled_atoms.json"):
    atoms = json.loads((ROOT / atoms_file).read_text(encoding="utf-8"))
    cases = json.loads((ROOT / "cases" / "scenario_cases.json").read_text(encoding="utf-8"))["cases"]
    scs = case_scenarios(cases)
    if everything:
        scs = [{**sc, "only": None, "id": sc["id"]} for sc in scs]
    return atoms, scs


# ---------------- checks ----------------
def _check():
    fam = {"whose": "us", "triggers": ["ownership_threshold"], "effect": "consent", "_unknown": ["threshold_pct is unknown", "threshold_op is unknown"]}
    atoms = {"c": {"coc": fam}}
    rv = lambda a, v: dict(contract_id="c", template="coc", atom=a, value=v)
    got = apply_reviews(atoms, [rv("threshold_pct", 50), rv("threshold_op", "gte")])["c"]["coc"]
    assert "_unknown" not in got and got["threshold_pct"] == 50 and atoms["c"]["coc"] is fam, "answers lift the blockers; input is untouched"
    assert apply_reviews(atoms, [rv("whose", "them")])["c"]["coc"].get("_unknown") is None, "a clause about the other party needs nothing else"
    got = apply_reviews(atoms, [rv("triggers", ["ownership_threshold", "retained_stake"])])["c"]["coc"]
    assert {atom_of(b) for b in got["_unknown"]} == {"threshold_pct", "threshold_op", "retained_holder", "retained_pct"}, "a new trigger asks for its numbers"
    assert "coc" not in apply_reviews(atoms, [rv("present", "no")])["c"]
    s = apply(State(), {"event": "share_sale", "buyer_pct": 60})
    f2 = {"whose": "us", "triggers": ["open_textured"], "effect": "consent", "_unknown": ["open_texture is unknown", "restricted is unknown"]}
    assert relevant_atoms("coc", f2, s) == ["open_texture"], "an atom that changes no reaction is not asked"
    f3 = {"_unknown": ["whose is unknown", "effects is unknown"], "triggers": ["ownership_threshold"], "threshold_pct": 50, "threshold_op": "gte"}
    assert relevant_atoms("coc", f3, s) == ["whose", "effects"], "two blockers that only matter together are both asked"
    f4 = {"whose": "us", "triggers": ["open_textured"], "open_texture": "magnitude", "effect": "consent"}
    assert firm(react("coc", f4, s))
    assert atom_of("initial_term_unit is silent") == "initial_term" and atom_of("silence guard: x") == "present"
    mem = sqlite3.connect(":memory:")
    add_review(mem, "c", "coc", "threshold_pct", 50, "someone", "read clause 4")
    got = load_reviews(mem)
    assert len(got) == 1 and got[0]["value"] == 50 and got[0]["scope"] == "contract" and got[0]["reviewer"] == "someone"
    got = apply_reviews(atoms, [dict(rv("threshold_pct", 10), kind="confirm")])["c"]["coc"]
    assert got == fam, "a confirmation changes nothing"
    assert expand("start", {"end_date": "2030-01-01"}) == [("end_date", "2030-01-01")] and expand("start", "2020-01-01") == [("start", "2020-01-01")]
    assert {("c", "term", "start"), ("c", "term", "initial_term")} <= confirmed([dict(contract_id="c", template="term", atom="end_date")])
    # load-bearing: a share sale of 60% against a 50% threshold with consent -> the threshold and the effect matter, 'whose' too
    f5 = {"whose": "us", "triggers": ["ownership_threshold"], "threshold_pct": 50, "threshold_op": "gte", "effect": "consent"}
    lb = load_bearing("coc", f5, s)
    assert {"present", "whose", "triggers", "threshold_pct", "effects"} <= set(lb) and "carve_outs" not in lb, lb
    low = apply(State(), {"event": "share_sale", "buyer_pct": 10})
    assert "effects" not in load_bearing("coc", f5, low), "nothing triggers, so the effect decides nothing"
    f6 = {"mode": "mutual", "start": "2020-01-01", "initial_term": [2, "years"], "notice": [30, "days"], "renewal_term": [1, "years"]}
    assert load_bearing("term", f6, State()) == ["present", "mode", "start", "initial_term"], "notice and renewal term decide nothing under mutual renewal"
    f7 = dict(f5, _optional_unknown=["carve_outs is unknown"])
    assert unsure(f7) == {"carve_outs"} and unsure(f5) == set()
    print("review self-check ok")


def main():
    ap = argparse.ArgumentParser(prog="python -m twin.review")
    ap.add_argument("cmd", choices=["queue", "show", "answer", "check"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--all", action="store_true", help="every case scenario against every contract")
    ap.add_argument("--no-confirm", action="store_true", help="ask blocking questions only (no confirmation of load-bearing atoms)")
    ap.add_argument("--by", default="")
    ap.add_argument("--reason", default="")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "check":
        return _check()
    con = sqlite3.connect(DB)
    atoms, scs = load_inputs(a.all)
    reviews = load_reviews(con)
    atoms = apply_reviews(atoms, reviews)
    qs = questions(atoms, scs, con, reviews=None if a.no_confirm else reviews)
    if a.cmd == "queue":
        for i, q in enumerate(qs, 1):
            tag = f"confirm {short(q.current)[:24]:24}" if q.kind == "confirm" else f"makes firm {q.firm_pairs:2}"
            print(f"{i:3}. {q.id:48} {tag}  affects {len(q.pairs):2}  [{', '.join(q.pairs[:6])}]")
        print(f"{len(qs)} questions")
    elif a.cmd == "show":
        print(next(q for q in qs if q.id == a.args[0]).markdown())
    else:
        qid, raw = a.args
        if not a.by:
            raise SystemExit("--by NAME is required")
        q = next((q for q in qs if q.id == qid), None)
        if q is None:
            raise SystemExit("no such open question; run `queue` for the ids")
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        kind = "answer"
        if q.kind == "confirm":
            kind = "confirm" if value == "confirm" or value == q.current else "correct"
            value = q.current if kind == "confirm" else value
        if q.atom in CHOICE and value not in CHOICE[q.atom] or q.atom in POWER and not set(value) <= set(POWER[q.atom]):
            raise SystemExit(f"not an allowed answer: {q.allowed}")
        gk, t, at = qid.split("|")
        for at2, v2 in expand(at, value) if kind != "confirm" else [(at, value)]:
            add_review(con, gk, t, at2, v2, a.by, a.reason, kind=kind)
        print(f"stored ({kind})")


if __name__ == "__main__":
    main()
