"""Score the rules against the independent scenario cases, using hand-labelled gold atoms.
Run from repo root: python -m tests.score_cases"""
import json
from datetime import date
from twin.core import State, apply
from twin.rules import TEMPLATES

cases = json.load(open("cases/scenario_cases.json", encoding="utf-8"))["cases"]
gold = json.load(open("cases/gold_atoms.json", encoding="utf-8"))
SAME = {"either": "both"}


def run(case):
    src = case["src"].split(" ")[0]
    if src.startswith("edgar:Amscan"):
        src = "edgar:Amscan"
    atoms = gold[src]
    s = State()
    for ev in case["scenario"]:
        s = apply(s, ev)
    fams = ["term"] if case["id"].startswith("N") else ["coc", "assign"]
    rs = [r for f in fams if f in atoms for r in TEMPLATES[f](atoms[f], s)]
    st = "TRIGGERED" if any(r.status == "TRIGGERED" for r in rs) else (
        "NEEDS_LAWYER" if any(r.status == "NEEDS_LAWYER" for r in rs) else "NOT_TRIGGERED")
    return st, rs


def check(case, st, rs):
    e = case["expected"]
    errs = []
    if st != e["status"]:
        errs.append(f"status {st} != {e['status']}")
    trig = [r for r in rs if r.status == "TRIGGERED"]
    for want in e.get("reactions", [e] if "kind" in e else []):
        m = [r for r in trig if r.kind == want["kind"]]
        if not m:
            errs.append(f"missing kind {want['kind']} (got {[r.kind for r in trig]})")
            continue
        r = m[0]
        if "due" in want and str(r.due) != want["due"]:
            errs.append(f"{want['kind']} due {r.due} != {want['due']}")
        if "holder" in want and r.holder and SAME.get(r.holder, r.holder) != SAME.get(want["holder"], want["holder"]) \
                and not (want["holder"] == "counterparty" and r.holder == "them"):
            errs.append(f"holder {r.holder} != {want['holder']}")
    return errs


if __name__ == "__main__":
    ok = 0
    for c in cases:
        st, rs = run(c)
        errs = check(c, st, rs)
        ok += not errs
        print(f"{'PASS' if not errs else 'FAIL'} {c['id']:4} {st:14} {'; '.join(errs) if errs else ''}  [{'; '.join(r.reason for r in rs)[:110]}]")
    print(f"\n{ok}/{len(cases)} cases match the independently written expectations")
