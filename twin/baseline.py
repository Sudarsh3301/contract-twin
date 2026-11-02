"""Direct-read baseline: the same open model reads the same candidate sentences and answers each scenario itself,
every time it is asked. Scored against the scenario cases, three independent runs plus a majority vote.

    python -m twin.baseline --dry      # prompt size and cost estimate, no calls
    python -m twin.baseline            # 37 cases x 3 runs -> out/baseline.json
    python -m twin.baseline --check    # offline self-check
"""
import argparse
import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import llm
from .compile import MAXLEN, retrieve
from .ingest import DB, ROOT

BUDGET = 2.0                 # extra spend allowed for this experiment, on top of what the shared total already holds
K_RUNS, TEMP = 3, 0.7
CACHE = ROOT / "out" / "cache_baseline"
STATUSES = ["TRIGGERED", "NOT_TRIGGERED", "NEEDS_LAWYER"]
KINDS = ["termination_right", "automatic_termination", "consent", "notice", "service_limitation", "deadline",
         "deadline_lapsed", "earliest_exit", "option_deadline", "expiry", "other"]
HOLDERS = ["us", "counterparty", "both"]
CASES = json.loads((ROOT / "cases" / "scenario_cases.json").read_text(encoding="utf-8"))
PORTFOLIO = json.loads((ROOT / "cases" / "portfolio.json").read_text(encoding="utf-8"))["contracts"]
CONVENTION = CASES["_note"].split("Day convention", 1)[1].split("Statuses:")[0].strip()
CONVENTION = "Day convention" + CONVENTION

SCHEMA = {"type": "object", "properties": {
    "status": {"type": "string", "enum": STATUSES},
    "reactions": {"type": "array", "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "holder": {"type": ["string", "null"], "enum": HOLDERS + [None]},
        "due": {"type": ["string", "null"]}}, "required": ["kind", "holder", "due"], "additionalProperties": False}},
    "sids": {"type": "array", "items": {"type": "integer"}},
    "reasoning": {"type": "string"}},
    "required": ["status", "reactions", "sids", "reasoning"], "additionalProperties": False}

SYSTEM = f"""You review ONE contract for a company and say how the contract reacts to a scenario. You are given numbered sentences from the contract, the party whose side we are on ("us"), and the scenario.

status:
- TRIGGERED: the contract gives some reaction under the scenario (consent needed, notice due, termination right, service limit, a date-driven deadline or expiry).
- NOT_TRIGGERED: on the text, the contract gives no reaction under the scenario.
- NEEDS_LAWYER: the text does not settle it (undefined or open-ended terms, a counterparty judgement standard, a missing document or number, or two reasonable readings). Do not guess.

reactions: when TRIGGERED, one entry per reaction. kind is one of: {', '.join(KINDS)}. holder is who holds the right or must act: us, counterparty or both (null if none). due is the date as YYYY-MM-DD, or null when the reaction has no date. Empty list unless TRIGGERED.
sids: ids of the sentences your answer rests on (only ids shown in brackets). reasoning: two sentences at most.

Unless the scenario says otherwise: no prior consent has been given, no buyer is a competitor, management does not change, no party is in default, there are no public holidays, and the date is 2027-01-01.
Ownership events give the cap table AFTER the event: buyer_pct is the largest single new holder; sponsor_pct_after, largest_other_pct_after are holdings after the event; float_pct is the share sold to the public.
{CONVENTION}"""


def contract_of(src):
    key = src.split(" ")[0]
    ids = [cid for cid, v in PORTFOLIO.items() if v["src"] == key]
    assert len(ids) == 1, (src, ids)
    return ids[0]


def prompt(con, case):
    cid = contract_of(case["src"])
    temps = ["term"] if case["id"].startswith("N") else ["coc", "assign"]
    sents = sorted({s for t in temps for s in retrieve(con, cid, t)})
    title = con.execute("SELECT title FROM contract WHERE id=?", (cid,)).fetchone()[0]
    body = "\n".join(f"[{sid}] {t[:MAXLEN]}" for sid, t in sents)
    return (f"Contract: {title}\nUs: {PORTFOLIO[cid]['our_party']}\n\nScenario (events in order):\n"
            + "\n".join(json.dumps(e) for e in case["scenario"]) + f"\n\nSentences:\n{body}"), {s for s, _ in sents}


# ---- scoring (same comparison as the rule scorer) -------------------------------------------------------------------

def to_reactions(ans):
    from datetime import date
    from .rules import Reaction
    if ans["status"] != "TRIGGERED":
        return []
    return [Reaction("TRIGGERED", r["kind"], r["holder"] or "",
                     date.fromisoformat(r["due"]) if r["due"] and len(r["due"]) == 10 else None) for r in ans["reactions"]]


def grade(case, ans):
    """-> (outcome, errors). outcome: correct | wrong_firm | abstain | over_firm | wrong_lawyer (never happens for NEEDS_LAWYER expected)."""
    from tests.score_cases import check
    errs = check(case, ans["status"], to_reactions(ans))
    want = case["expected"]["status"]
    if not errs:
        return "correct", errs
    if ans["status"] == "NEEDS_LAWYER":
        return "abstain", errs
    if want == "NEEDS_LAWYER":
        return "over_firm", errs
    return "wrong_firm", errs


def signature(ans):
    return json.dumps([ans["status"], sorted((r["kind"], r["holder"], r["due"]) for r in ans["reactions"])]
                      if ans["status"] == "TRIGGERED" else [ans["status"]])


def majority(answers):
    """Same answer (status, and reactions when triggered) from >= 2 of 3 runs wins; otherwise NEEDS_LAWYER."""
    sig, n = Counter(signature(a) for a in answers).most_common(1)[0]
    if n >= 2:
        return next(a for a in answers if signature(a) == sig)
    return {"status": "NEEDS_LAWYER", "reactions": [], "sids": [], "reasoning": "runs disagree"}


def tally(rows):
    c = Counter(o for o, _ in rows)
    return {"correct": c["correct"], "wrong_firm": c["wrong_firm"], "over_firm": c["over_firm"], "abstain": c["abstain"],
            "n": len(rows)}


# ---- cost model -------------------------------------------------------------------------------------------------------

def cost_table(per_call, compile_per_contract):
    rows = []
    for n in (30, 1000):
        for s in (1, 10, 100):
            rows.append({"contracts": n, "scenarios": s, "baseline_1run_usd": round(n * s * per_call, 2),
                         "baseline_vote3_usd": round(n * s * per_call * K_RUNS, 2),
                         "twin_usd": round(n * compile_per_contract, 2)})
    return rows


def run(dry=False, workers=8):
    con = sqlite3.connect(DB)
    jobs = [(c, prompt(con, c)) for c in CASES["cases"]]
    if dry:
        chars = sum(len(SYSTEM) + len(u) for _, (u, _) in jobs)
        print(f"{len(jobs)} cases x {K_RUNS}: ~{chars / 4 * K_RUNS / 1e6:.2f}M input tokens, est. ${chars / 4 * K_RUNS * 0.3e-6 + len(jobs) * K_RUNS * 1500 * 1.3e-6:.2f}")
        return
    cap = llm.spent() + BUDGET

    def one(job):
        (case, (user, cand)), seed = job
        r = llm.ask(llm.ANSWERER, SYSTEM, user, SCHEMA, seed=seed, temperature=TEMP, prompt_version="baseline1",
                    cache_dir=CACHE, budget=cap)
        a = r["data"]
        a["sids"] = [s for s in a["sids"] if s in cand]      # a cited id outside the shown sentences is dropped
        return a, r["cost"], r["tokens"]

    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(one, [(j, s) for j in jobs for s in range(K_RUNS)]))
    cases_out, per_run, cost, toks = [], [[] for _ in range(K_RUNS)], 0.0, [0, 0]
    maj, agree_status, agree_all = [], 0, 0
    for i, (case, _) in enumerate(jobs):
        rs = res[i * K_RUNS:(i + 1) * K_RUNS]
        answers = [a for a, _, _ in rs]
        grades = [grade(case, a) for a in answers]
        m = majority(answers)
        gm = grade(case, m)
        for k in range(K_RUNS):
            per_run[k].append(grades[k])
        maj.append(gm)
        agree_status += len({a["status"] for a in answers}) == 1
        agree_all += len({signature(a) for a in answers}) == 1
        cost += sum(c for _, c, _ in rs)
        toks = [toks[0] + sum(t[0] or 0 for _, _, t in rs), toks[1] + sum(t[1] or 0 for _, _, t in rs)]
        cases_out.append({"id": case["id"], "expected": case["expected"]["status"],
                          "runs": [{"answer": a, "outcome": g[0], "errors": g[1]} for a, g in zip(answers, grades)],
                          "majority": {"answer": m, "outcome": gm[0], "errors": gm[1]}})
    ncalls = len(res)
    per_call = cost / ncalls
    compile_pc = 1.187 / 30      # measured: tasks/reports/T03.md, fresh-cache compile of 30 contracts x 3 families
    out = {"model": llm.ANSWERER, "runs": K_RUNS, "temperature": TEMP,
           "per_run": [tally(r) for r in per_run], "majority": tally(maj),
           "agreement": {"same_status": f"{agree_status}/{len(jobs)}", "same_status_kind_holder_due": f"{agree_all}/{len(jobs)}"},
           "cost": {"total_usd": round(cost, 4), "calls": ncalls, "per_call_usd": round(per_call, 5),
                    "mean_tokens_in_out": [round(toks[0] / ncalls), round(toks[1] / ncalls)],
                    "twin_compile_per_contract_usd": round(compile_pc, 4), "table": cost_table(per_call, compile_pc)},
           "cases": cases_out}
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / "baseline.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("per_run", "majority", "agreement")}, indent=1))
    print(f"cost ${cost:.3f} for {ncalls} calls; total spend ${llm.spent():.3f}")
    for r in out["cost"]["table"]:
        print(r)


def _check():
    a = lambda s, k=None, d=None, h="counterparty": {"status": s, "sids": [], "reasoning": "",
                                                    "reactions": [{"kind": k, "holder": h, "due": d}] if k else []}
    case = next(c for c in CASES["cases"] if c["id"] == "R01")
    assert grade(case, a("TRIGGERED", "termination_right"))[0] == "correct"
    assert grade(case, a("NOT_TRIGGERED"))[0] == "wrong_firm"
    assert grade(case, a("NEEDS_LAWYER"))[0] == "abstain"
    assert grade(next(c for c in CASES["cases"] if c["id"] == "R08"), a("NOT_TRIGGERED"))[0] == "over_firm"
    n01 = next(c for c in CASES["cases"] if c["id"] == "N01")
    assert grade(n01, a("TRIGGERED", "deadline", "2012-09-21", "both"))[0] == "correct"
    assert grade(n01, a("TRIGGERED", "deadline", "2012-09-22", "both"))[0] == "wrong_firm"
    assert majority([a("TRIGGERED", "consent"), a("TRIGGERED", "consent"), a("NOT_TRIGGERED")])["status"] == "TRIGGERED"
    assert majority([a("TRIGGERED", "consent"), a("NOT_TRIGGERED"), a("NEEDS_LAWYER")])["status"] == "NEEDS_LAWYER"
    assert contract_of("edgar:Amscan ABL Amendment No.2 (2011)") == "amscan_2011_credit"
    con = sqlite3.connect(DB)
    u, cand = prompt(con, CASES["cases"][0])
    assert cand and "RAE Systems" in u and "expected" not in u
    print("baseline self-check ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    x = ap.parse_args()
    _check() if x.check else run(x.dry, x.workers)
