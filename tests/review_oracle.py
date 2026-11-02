"""Review-queue experiment: answer the ranked questions with the hand-labelled gold atoms as the lawyer, re-run the
scenario cases after every answer, and record cases correct, wrong firm answers and abstentions along the way.

    python -m tests.review_oracle [--start compiled|blank] [--out out/review_curve.json]

compiled = start from out/compiled_atoms.json. blank = start from the gold clauses with every atom unknown.
The gold atoms only answer questions; they never decide a reaction. Writes the curve to out/review_curve.json.
"""
import json
import sys
from twin import review
from twin.review import apply_reviews, get_effects, questions
from tests import score_cases as sc

GOLD = {k: v for k, v in sc.gold.items() if not k.startswith("_")}
ABSTAIN = object()                      # the gold has no value for this atom
SETS = {"triggers", "transaction_types", "carve_outs", "deems"}


def lawyer(gk, template, atom):
    """The gold value for one atom, in the shape the review table stores. ABSTAIN when the gold says nothing."""
    g = GOLD.get(gk, {}).get(template)
    if atom == "present":
        return "yes" if g else "no"
    if g is None:
        return "no"                      # no such clause in the gold: the reviewer removes the clause
    if atom == "effects":
        return get_effects(g) or ABSTAIN
    if atom.startswith("unless_consented:"):
        return any(e["effect"] == atom.split(":", 1)[1] and e.get("unless_consented") for e in get_effects(g))
    if atom in g:
        return g[atom]
    if atom in review.BOOLS:
        return False
    return [] if atom in SETS else ABSTAIN


def score(atoms):
    tally = {"correct": 0, "wrong firm": 0, "abstain": 0, "other error": 0}
    wrong = []
    for c in sc.cases:
        sc.gold = atoms
        st, rs = sc.run(c)
        errs = sc.check(c, st, rs)
        want = c["expected"]["status"]
        key = "correct" if not errs else "wrong firm" if st != "NEEDS_LAWYER" and st != want else \
            "abstain" if st == "NEEDS_LAWYER" and want != st else "other error"
        tally[key] += 1
        if key == "wrong firm":
            wrong.append(c["id"])
    sc.gold = GOLD
    return tally, wrong


def start_atoms(kind):
    if kind == "compiled":
        return json.load(open("out/compiled_atoms.json", encoding="utf-8"))
    return {gk: {t: {"_unknown": ["present is unknown"]} for t in fams} for gk, fams in GOLD.items()}


def experiment(kind):
    start = start_atoms(kind)
    scenarios = review.case_scenarios(sc.cases)
    reviews, skipped, asked, curve = [], set(), [], []
    def point(label):
        tally, wrong = score(apply_reviews(start, reviews))
        curve.append(dict(answers=len(reviews), after=label, **tally, wrong_firm_cases=wrong))
    point("start")
    first = questions(apply_reviews(start, reviews), scenarios)
    ids = [(q.contract, q.template, q.atom) for q in first]
    assert len(ids) == len(set(ids)), "one question per contract and atom"
    assert all(q.cited and q.preview and all(t for _, t in q.cited) for q in first), "every question has cited text and an impact preview"
    while True:
        qs = [q for q in questions(apply_reviews(start, reviews), scenarios) if q.id not in skipped]
        if not qs:
            break
        q = qs[0]
        v = lawyer(q.contract, q.template, q.atom)
        if v is ABSTAIN:
            skipped.add(q.id)
            continue
        if v == "no" and q.atom != "present":       # no such clause: store it as the clause answer
            reviews.append(dict(contract_id=q.contract, template=q.template, atom="present", value="no", reviewer="gold"))
        else:
            reviews.append(dict(contract_id=q.contract, template=q.template, atom=q.atom, value=v, reviewer="gold"))
        asked.append(q.id)
        point(q.id)
    # determinism: the same reviews on the same atoms give the same outcome twice
    a, b = score(apply_reviews(start, reviews)), score(apply_reviews(start, reviews))
    final = questions(apply_reviews(start, reviews), scenarios)
    best = max(p["correct"] for p in curve)
    return dict(start=kind, answers_to_max=next(p["answers"] for p in curve if p["correct"] == best), max_correct=best,
                cases=len(sc.cases), questions_answered=len(asked), questions_skipped_no_gold_value=sorted(skipped),
                open_questions_left=[q.id for q in final], deterministic=a == b, curve=curve)


if __name__ == "__main__":
    kinds = [sys.argv[sys.argv.index("--start") + 1]] if "--start" in sys.argv else ["compiled", "blank"]
    out = {k: experiment(k) for k in kinds}
    for k, r in out.items():
        print(f"start={k}: {r['questions_answered']} answers, max {r['max_correct']}/{r['cases']} correct after {r['answers_to_max']} answers, "
              f"skipped {len(r['questions_skipped_no_gold_value'])}, left {len(r['open_questions_left'])}, deterministic {r['deterministic']}")
        for p in r["curve"]:
            print(f"  {p['answers']:3} correct {p['correct']:2} wrong firm {p['wrong firm']} abstain {p['abstain']:2} other {p['other error']}  {p['after']}")
    path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "out/review_curve.json"
    json.dump(out, open(path, "w", encoding="utf-8"), indent=1)
