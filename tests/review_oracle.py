"""Review-queue experiment: answer the ranked questions with the hand-labelled gold atoms as the lawyer, re-run the
scenario cases after every action, and record cases correct, wrong firm answers, abstentions and pending confirmations.

    python -m tests.review_oracle [--start compiled|blank] [--no-confirm] [--out out/review_curve.json]

compiled = start from out/compiled_atoms.json. blank = start from the gold clauses with every atom unknown.
Lawyer actions: answer (a blocking question), confirm (a load-bearing compiled atom is right), correct (it is wrong).
An outcome whose load-bearing atoms are not all confirmed counts as pending, not as firm. --no-confirm turns that off.
The gold atoms only answer questions; they never decide a reaction. Writes the curve to out/review_curve.json.
"""
import json
import sys
from twin import review
from twin.review import apply_reviews, get_atom, get_effects, questions
from twin.rules import expiry
from tests import score_cases as sc

GOLD = {k: v for k, v in sc.gold.items() if not k.startswith("_")}
ABSTAIN = object()                      # the gold has no value for this atom
SETS = {"triggers", "transaction_types", "carve_outs", "deems"}
DEFAULTS = {"threshold_op": "gte", "competitor_threshold_pct": 5}      # what the rules assume when an atom is not stated
SPAN = {"start", "initial_term", "end_date"}                           # the atoms that together fix the end of the initial term


def lawyer(gk, template, atom):
    """The gold value for one atom, in the shape the review table stores it. ABSTAIN when the gold says nothing."""
    g = GOLD.get(gk, {}).get(template)
    if atom == "present":
        return "yes" if g else "no"
    if g is None:
        return "no"                      # no such clause in the gold: the reviewer removes the clause
    if atom == "effects":
        return get_effects(g) or ABSTAIN
    if atom.startswith("unless_consented:"):
        return any(e["effect"] == atom.split(":", 1)[1] and e.get("unless_consented") for e in get_effects(g))
    if atom in SPAN and atom != "end_date" and "end_date" in g and atom not in g:
        return {"end_date": g["end_date"]}      # the contract states an end date, no start or length
    if atom == "qualifying_ipo":
        return g.get(atom) or False
    if atom in g:
        return g[atom]
    if atom in DEFAULTS:
        return DEFAULTS[atom]
    if atom in review.TRUE_FALSE:
        return False
    return [] if atom in SETS else ABSTAIN


def canon(atom, v):
    """A value in a form where equal readings compare equal."""
    if atom == "effects":
        es = [dict(effect=e["effect"], holder=e.get("effect_holder") or "", notice=e.get("notice"), unless=bool(e.get("unless_consented")))
              for e in v]
        return sorted(json.dumps(canon("", e), sort_keys=True) for e in es)
    if atom in SETS:
        return sorted(v)
    if atom == "qualifying_ipo":
        v = v or False
    if isinstance(v, dict):
        return {k: canon("", x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [canon("", x) for x in v]
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def judge(q, fam):
    """(kind, value) the gold lawyer gives for a question, or None when the gold has no value. kind: answer / confirm / correct."""
    v = lawyer(q.contract, q.template, q.atom)
    if q.kind == "blocking":
        return None if v is ABSTAIN else ("answer", v)
    g = GOLD.get(q.contract, {}).get(q.template)
    if g is None and q.atom in SETS | review.TRUE_FALSE:
        v = [] if q.atom in SETS else False         # no gold clause reads as "nothing listed": a compiled empty list is right
    if q.atom in SPAN and g:                    # compare what the fields amount to: the date the initial term ends
        try:
            want, have = expiry(g), expiry(fam)
        except (KeyError, TypeError, ValueError):
            want = have = None
        if want is not None:
            if have == want:
                return ("confirm", q.current)
            return ("correct", str(want) if q.atom == "end_date" else {"end_date": str(want)})
    if v is ABSTAIN:
        return None
    if v != "no" and canon(q.atom, v) == canon(q.atom, q.current):
        return ("confirm", q.current)
    return ("correct", v)


def score(atoms, pending=frozenset()):
    """Tally the cases. A case with a firm status and an unconfirmed load-bearing atom is pending, whatever the status is."""
    tally = {"correct": 0, "wrong firm": 0, "abstain": 0, "pending": 0, "other error": 0}
    wrong, hidden = [], []
    for c in sc.cases:
        sc.gold = atoms
        st, rs = sc.run(c)
        errs = sc.check(c, st, rs)
        want = c["expected"]["status"]
        key = "correct" if not errs else "wrong firm" if st != "NEEDS_LAWYER" and st != want else \
            "abstain" if st == "NEEDS_LAWYER" and want != st else "other error"
        if c["id"] in pending and st != "NEEDS_LAWYER":
            if key in ("wrong firm", "other error"):
                hidden.append(c["id"])           # a wrong answer the confirmation step is holding back
            key = "pending"
        tally[key] += 1
        if key == "wrong firm":
            wrong.append(c["id"])
    sc.gold = GOLD
    return tally, wrong, hidden


def start_atoms(kind):
    if kind == "compiled":
        return json.load(open("out/compiled_atoms.json", encoding="utf-8"))
    return {gk: {t: {"_unknown": ["present is unknown"]} for t in fams} for gk, fams in GOLD.items()}


def experiment(kind, confirm=True):
    start = start_atoms(kind)
    scenarios = review.case_scenarios(sc.cases)
    reviews, skipped, curve, found = [], set(), [], []
    n = {"answer": 0, "confirm": 0, "correct": 0}

    def state():
        atoms = apply_reviews(start, reviews)
        return atoms, questions(atoms, scenarios, reviews=reviews if confirm else None)

    def point(qs, label):
        pend = {p for q in qs if q.kind == "confirm" for p in q.pairs}
        tally, wrong, hidden = score(apply_reviews(start, reviews), pend)
        curve.append(dict(actions=len(reviews), answers=n["answer"], confirmations=n["confirm"], corrections=n["correct"], after=label,
                          **tally, wrong_firm_cases=wrong, held_back_wrong_cases=hidden))
    atoms, qs = state()
    point(qs, "start")
    assert len({(q.contract, q.template, q.atom) for q in qs}) == len(qs), "one question per contract and atom"
    assert all(q.cited and q.preview and all(t for _, t in q.cited) for q in qs), "every question has cited text and an impact preview"
    for _ in range(3000):
        todo = [q for q in qs if q.id not in skipped]
        if not todo:
            break
        q = todo[0]
        j = judge(q, atoms.get(q.contract, {}).get(q.template, {}))
        if j is None:
            skipped.add(q.id)
            continue
        k, v = j
        if v == "no" and q.atom != "present":       # the gold has no such clause: store it as the clause answer
            reviews.append(dict(contract_id=q.contract, template=q.template, atom="present", value="no", reviewer="gold",
                                kind="answer" if k == "answer" else "correct"))
        else:
            for at, v2 in review.expand(q.atom, v) if k != "confirm" else [(q.atom, v)]:
                reviews.append(dict(contract_id=q.contract, template=q.template, atom=at, value=v2, reviewer="gold", kind=k))
        n[k] += 1
        if k == "correct":
            found.append(dict(id=q.id, compiled=q.current, gold="no such clause in the gold" if v == "no" else v, agreement=list(q.agreement)))
        atoms, qs = state()
        point(qs, f"{k}: {q.id}")
    # determinism: the same reviews on the same atoms give the same outcome twice
    atoms, qs = state()
    pend = {p for x in qs if x.kind == "confirm" for p in x.pairs}
    a, b = score(apply_reviews(start, reviews), pend), score(apply_reviews(start, reviews), pend)
    best = max(p["correct"] for p in curve)
    at_max = next(p for p in curve if p["correct"] == best)
    asked = n["confirm"] + n["correct"]
    return dict(start=kind, confirm_on=confirm, max_correct=best, cases=len(sc.cases), actions_to_max=at_max["actions"],
                at_max=dict(answers=at_max["answers"], confirmations=at_max["confirmations"], corrections=at_max["corrections"]),
                totals=dict(n, actions=len(reviews)), wrong_compiled_atoms_found=found,
                hidden_error_rate=f"{n['correct']}/{asked}" if asked else "0/0",
                questions_skipped_no_gold_value=sorted(skipped), open_questions_left=[q.id for q in qs],
                max_wrong_firm=max(p["wrong firm"] for p in curve), deterministic=a == b, curve=curve)


if __name__ == "__main__":
    kinds = [sys.argv[sys.argv.index("--start") + 1]] if "--start" in sys.argv else ["compiled", "blank"]
    confirm = "--no-confirm" not in sys.argv
    out = {k: experiment(k, confirm) for k in kinds}
    for k, r in out.items():
        print(f"start={k} confirm={confirm}: max {r['max_correct']}/{r['cases']} correct after {r['actions_to_max']} actions {r['at_max']}; "
              f"totals {r['totals']}; wrong compiled atoms found {r['hidden_error_rate']}; max wrong firm {r['max_wrong_firm']}; "
              f"skipped {len(r['questions_skipped_no_gold_value'])}, left {len(r['open_questions_left'])}, deterministic {r['deterministic']}")
        for p in r["curve"]:
            print(f"  {p['actions']:3} (a{p['answers']} c{p['confirmations']} x{p['corrections']}) correct {p['correct']:2} wrong firm {p['wrong firm']} "
                  f"abstain {p['abstain']:2} pending {p['pending']:2} other {p['other error']}  {p['after']}")
    path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else \
        "out/review_curve.json" if confirm else "out/review_curve_noconfirm.json"
    json.dump(out, open(path, "w", encoding="utf-8"), indent=1)
