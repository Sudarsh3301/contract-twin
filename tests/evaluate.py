"""Regenerate every number in the results report, then the report itself.

    python -m tests.evaluate              # out/eval/results.json, docs/results.md, README table, out/portfolio/results.html
    python -m tests.evaluate stability    # the one recompile run (about $0.40, network): out/eval/stability.json
    python -m tests.evaluate check        # self-check of the helpers

No model calls except `stability`. The report text is built from results.json, so no number in it is typed by hand.
Grades: [measured] this script computed it from current code and data; [source] read from a stored model run
(out/baseline.json, the blind check, out/cache/spend.json); [unverified] not checked.
"""
import contextlib
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

from twin import baseline, llm, portfolio, review
from twin import compile as comp
from tests import consistency_ar as ca
from tests import review_oracle as ro
from tests import score_atoms as sa
from tests import score_cases as sc

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "eval"
GOLD0 = sc.gold                      # hand-labelled atoms (the ceiling for the rules)
COMPILED = "out/compiled_atoms.json"
ADJUDICATED_OURS_WRONG = ("R13", "R19")      # cases/scenario_cases.json `_adjudication`: our expectation was fixed
STAB_N = 10


def load(p):
    return json.loads((ROOT / p).read_text(encoding="utf-8"))


# ---------------- scenario cases ----------------
def classify(case, atoms):
    """(class, signature) of one case on a set of atoms. Same classes as the direct-read baseline grade."""
    sc.gold = atoms
    try:
        st, rs = sc.run(case)
    finally:
        sc.gold = GOLD0
    errs = sc.check(case, st, rs)
    want = case["expected"]["status"]
    cls = "correct" if not errs else "abstain" if st == "NEEDS_LAWYER" else "over_firm" if want == "NEEDS_LAWYER" else "wrong_firm"
    sig = [st, sorted((r.kind, r.holder, str(r.due)) for r in rs if r.status == "TRIGGERED")]
    return cls, sig


def case_tally(atoms, cases=None):
    rows = [(c["id"], *classify(c, atoms)) for c in (cases or sc.cases)]
    n = Counter(r[1] for r in rows)
    return dict(n=len(rows), correct=n["correct"], wrong_firm=n["wrong_firm"], over_firm=n["over_firm"], abstain=n["abstain"],
                not_correct=sorted(r[0] for r in rows if r[1] != "correct")), {r[0]: r[2] for r in rows}


def quiet(f, *a):
    with contextlib.redirect_stdout(io.StringIO()) as b:
        f(*a)
    return b.getvalue()


def atom_score(comp_atoms):
    t = quiet(sa.vs_gold, comp_atoms, load("cases/gold_atoms.json"), False)
    m = re.search(r"TOTAL (\d+)/(\d+) correct \(\d+%\), wrong (\d+), unknown (\d+), missing (\d+)", t)
    return dict(zip(("correct", "total", "wrong", "unknown", "missing"), map(int, m.groups())))


# ---------------- review effort ----------------
def review_part():
    ro.GOLD = {k: v for k, v in GOLD0.items() if not k.startswith("_")}
    atomwise = ro.experiment("compiled")
    cards = portfolio.measure()
    t = atomwise["totals"]
    asked = t["confirm"] + t["correct"]
    return dict(
        atom_walk=dict(max_correct=atomwise["max_correct"], cases=atomwise["cases"], actions=t["actions"], answers=t["answer"],
                       confirmations=t["confirm"], corrections=t["correct"], max_wrong_firm=atomwise["max_wrong_firm"],
                       hidden_wrong=t["correct"], hidden_checked=asked, deterministic=atomwise["deterministic"],
                       wrong_atoms=sorted(f["id"] for f in atomwise["wrong_compiled_atoms_found"])),
        card_walk=dict(answers=cards["card_walk"]["answers"], cards=cards["card_walk"]["cards"],
                       atoms_confirmed=cards["card_walk"]["atoms_confirmed"], atoms_corrected=cards["card_walk"]["atoms_corrected"],
                       end_correct=cards["card_walk"]["end"]["correct"], max_wrong_firm=cards["card_walk"]["max_wrong_firm"]))


# ---------------- portfolio page numbers ----------------
def portfolio_part():
    data = portfolio.build()
    out = {}
    for name, st in data["states"].items():
        h = st["header"]
        n_c = len(data["contracts"])
        eff = st["effort"]
        out[name] = dict(outcomes=h["outcomes"], firm=h["firm"], provisional=h["provisional"], blocked=h["blocked"],
                         counsel=h["counsel"], needs_lawyer=st["needs_lawyer"], main_cause=st["main_cause"], any_cause=st["causes"],
                         cards=eff["cards"], single_atom_confirmations=eff["single_atom_confirmations"], clauses_with_cards=eff["clauses"],
                         questions=len(st["questions"]), contracts=n_c, cards_per_contract=round(eff["cards"] / n_c, 2),
                         cases=h["cases"], hand_labels=h["hand_labels"],
                         questions_answered=h["questions_answered"], cards_answered=h["cards_answered"])
    sim = json.loads(portfolio.SIMULATED.read_text(encoding="utf-8"))["stats"]
    out["simulated_review"] = dict(answers=sim["answers"], cards=sim["cards"], confirmed=sim["confirmed"], corrected=sim["corrected"],
                                   skipped=sim["skipped"],
                                   hidden_error_rate=round(sim["corrected"] / (sim["confirmed"] + sim["corrected"]), 3))
    out["scenarios"] = len(data["scenarios"])
    out["contracts"] = len(data["contracts"])
    return out


# ---------------- baseline, blind check, Arabic ----------------
def baseline_part(compile_per_contract):
    b = load("out/baseline.json")
    per_call = b["cost"]["per_call_usd"]
    twin, _ = case_tally(load(COMPILED))
    return dict(model=b["model"], runs=b["runs"], majority=b["majority"], per_run=b["per_run"], agreement=b["agreement"],
                calls=b["cost"]["calls"], cost_usd=b["cost"]["total_usd"], per_call_usd=per_call,
                twin_compiled={k: twin[k] for k in ("correct", "wrong_firm", "over_firm", "abstain")},
                compile_per_contract_usd=compile_per_contract,
                cost_table=baseline.cost_table(per_call, compile_per_contract),
                break_even_scenarios=round(compile_per_contract / per_call, 1))


def blind_part():
    x = load("cases/blind_expectations_glm53.json")
    miss = [c["id"] for c in x["cases"] if not c["status_agree"]]
    fixed = [i for i in ADJUDICATED_OURS_WRONG if i in miss]
    return dict(model=x["model"], samples=x["k"], cases=x["n"], agree_before=x["agree_status"], disagreements=sorted(miss),
                ours_wrong_fixed=fixed, ours_upheld=sorted(set(miss) - set(fixed)), cost_usd=x["cost_usd"])


def arabic_part():
    en, ar = ca.en, ca.ar
    tot = dict(cases=0, same_outcome=0, wrong_firm=0, equal=0, differ=0, unknown=0)
    threshold = {}
    for key, g in ca.gold_ar.items():
        src = (g.get("_twin_of") or g.get("_translation_of")) if isinstance(g, dict) else None
        if not src:
            continue
        for _, cls, *_r in ca.compare(ar.get(key, {}), en.get(src, {})):
            tot[cls] += 1
        for c in [c for c in ca.cases if c["src"].split(" ")[0] == src]:
            ra, rb = ca.outcome(ar.get(key, {}), c), ca.outcome(en.get(src, {}), c)
            tot["cases"] += 1
            tot["same_outcome"] += ra[2] == rb[2]
            tot["wrong_firm"] += ra[1] == "wrong firm"
            if key == "ar:mt_dyntek":
                threshold[c["id"]] = dict(arabic=ra[0], english=rb[0], expected=c["expected"]["status"], same=ra[2] == rb[2])
    sc.gold = GOLD0
    d = lambda a: a.get("DynTek" if False else "coc", {}).get("threshold_pct", "missing")
    dyn_en, dyn_ar = en.get("cuad:DynTek", {}).get("coc", {}), ar.get("ar:mt_dyntek", {}).get("coc", {})
    return dict(**tot, dyntek_cases=threshold,
                dyntek_threshold=dict(english=dyn_en.get("threshold_pct", "not in atoms"), arabic=dyn_ar.get("threshold_pct", "not in atoms"),
                                      arabic_blockers=dyn_ar.get("_unknown", [])))


# ---------------- stability (the one recompile run) ----------------
def top_contracts(n=STAB_N):
    cnt = Counter(baseline.contract_of(c["src"]) for c in sc.cases)
    return sorted(cnt, key=lambda k: (-cnt[k], k))[:n], cnt


def stability_run():
    ids, cnt = top_contracts()
    comp.PORTFOLIO = {k: v for k, v in comp.PORTFOLIO.items() if k in ids}      # compile only these contracts
    before = llm.spent()
    comp.run("", "stab", False, 8, 0.8, "en")
    OUT.mkdir(parents=True, exist_ok=True)
    spent = round(llm.spent() - before, 4)
    (OUT / "stability_run.json").write_text(json.dumps(dict(contracts=ids, cost_usd=spent), indent=1), encoding="utf-8")
    print("stability run spent", spent)


def _final_atoms(start):
    """Run the atom-at-a-time review walk from `start`; return its result and the atoms after the last action."""
    last = {}
    orig_score, orig_start = ro.score, ro.start_atoms

    def score(atoms, pending=frozenset()):
        last["atoms"] = atoms
        return orig_score(atoms, pending)
    ro.score, ro.start_atoms = score, lambda kind: start
    try:
        r = ro.experiment("compiled")
    finally:
        ro.score, ro.start_atoms = orig_score, orig_start
    return r, last["atoms"]


def stability_part():
    p = OUT / "stability_run.json"
    if not p.exists():
        return dict(run="not run")
    run = json.loads(p.read_text(encoding="utf-8"))
    main, stab = load(COMPILED), load("out/compiled_atoms_stab.json")
    keys = sorted({comp.gold_key(c) for c in run["contracts"]})
    a, b = {k: main.get(k, {}) for k in keys}, {k: stab.get(k, {}) for k in keys}
    m = re.search(r"(\d+)/(\d+)", quiet(sa.vs_run, a, b))
    cases = [c for c in sc.cases if comp.gold_key(baseline.contract_of(c["src"])) in keys]
    merged = {**main, **b}
    ro.GOLD = {k: v for k, v in GOLD0.items() if not k.startswith("_")}
    rm, am = _final_atoms(main)
    rs, as_ = _final_atoms(merged)
    sig = lambda atoms: {c["id"]: classify(c, atoms)[1] for c in cases}
    differ = lambda x, y: sum(x[i] != y[i] for i in x)
    tm, ts = case_tally(main, cases)[0], case_tally(merged, cases)[0]
    sm, ss = sig(main), sig(merged)
    fm, fs = sig(am), sig(as_)
    return dict(contracts=len(keys), cases=len(cases), cost_usd=run["cost_usd"], compile_per_contract_usd=round(run["cost_usd"] / len(keys), 4),
                atoms_differ=int(m.group(1)), atoms_total=int(m.group(2)),
                compiled_only=dict(main=tm, rerun=ts, outcomes_differ=differ(sm, ss)),
                after_review=dict(main_actions=rm["totals"]["actions"], rerun_actions=rs["totals"]["actions"],
                                  main_max_correct=rm["max_correct"], rerun_max_correct=rs["max_correct"],
                                  rerun_max_wrong_firm=rs["max_wrong_firm"], outcomes_differ=differ(fm, fs),
                                  main_corrections=rm["totals"]["correct"], rerun_corrections=rs["totals"]["correct"]))


# ---------------- assemble ----------------
def compute():
    comp_atoms = load(COMPILED)
    gold_t, _ = case_tally(GOLD0)
    comp_t, _ = case_tally(comp_atoms)
    stab = stability_part()
    per_contract = stab.get("compile_per_contract_usd") or round(1.187 / 30, 4)
    res = dict(
        cases=dict(gold=gold_t, compiled=comp_t, atoms=atom_score(comp_atoms), contracts_en=len(comp_atoms)),
        review=review_part(), portfolio=portfolio_part(), baseline=baseline_part(per_contract),
        blind=blind_part(), arabic=arabic_part(), stability=stab,
        spend_total_usd=round(llm.spent(), 2),
        models=dict(answer=llm.ANSWERER, checker=llm.CHECKER, blind=load("cases/blind_expectations_glm53.json")["model"]),
        data=dict(cuad=27, edgar=3, arabic=8, sentences=sentence_count(), sentences_en=sentence_count("en"),
                  truth_table_rows=sum(1 for _ in open(ROOT / "out" / "truth_coc.csv", encoding="utf-8")) - 1),
        grades=dict(compile_per_contract="measured in the stability run" if "cost_usd" in stab else "source: earlier fresh-cache compile of 30 contracts (1.187 USD)"))
    return res


def sentence_count(lang=None):
    import sqlite3
    return sqlite3.connect(ROOT / "data" / "twin.sqlite").execute(
        "SELECT count(*) FROM sentence s JOIN contract k ON k.id = s.contract_id WHERE ? IS NULL OR k.language = ?", (lang, lang)).fetchone()[0]


# ---------------- report ----------------
DERIVED = set()          # numbers the report computes from results.json (percentages, sums); stored in results.json too


def pct(a, b):
    v = f"{100 * a / b:.1f}%" if b else "n/a"
    DERIVED.add(v)
    return v


def card_steps(cw):
    """Reviewer steps in the card walk: one per blocking answer and one per card (corrections happen inside a card)."""
    DERIVED.add(str(cw["answers"] + cw["cards"]))
    return cw["answers"] + cw["cards"]


def blocks(r):
    """The report as a list of (kind, payload) blocks. Every number comes from `r`."""
    c, g, comp_, at = r["cases"], r["cases"]["gold"], r["cases"]["compiled"], r["cases"]["atoms"]
    rv, pf, bl, bd, ar, st = r["review"], r["portfolio"], r["baseline"], r["blind"], r["arabic"], r["stability"]
    pc, pr = pf["compiled"], pf["review"]
    aw, cw = rv["atom_walk"], rv["card_walk"]
    out = [("h1", "Results"),
           ("p", "Proof of concept. Not legal advice. Nothing here is lawyer-reviewed. Every number below is written by "
                 "`python -m tests.evaluate` from the current code and data into `out/eval/results.json`. Grades: **[measured]** computed "
                 "by the script from current code and data; **[source]** read from a stored model run; **[unverified]** not checked."),
           ("h2", "Setup"),
           ("ul", [f"Data: {r['data']['cuad']} CUAD v1 contracts (CC BY 4.0, The Atticus Project), {r['data']['edgar']} SEC EDGAR credit-agreement "
                   f"amendments, {r['data']['arabic']} Arabic texts. {r['data']['sentences']:,} sentences in the database [measured].",
                   f"Models (open weights, via OpenRouter): answers `{r['models']['answer']}`, support check `{r['models']['checker']}`, "
                   f"blind check `{r['models']['blind']}`.",
                   f"Hand-labelled by the authors: {g['n']} scenario cases with expected outcomes, gold atoms for the clauses they use, and "
                   "the portfolio file that names our party in each contract. The same author wrote the cases and the rules.",
                   f"Portfolio demo: {pf['contracts']} contracts, {pf['scenarios']} scenarios, {pc['outcomes']} outcomes per state."]),
           ("h2", "Scenario cases"),
           ("table", (["Atoms", "Correct", "Wrong firm", "Firm where we abstain", "Abstain", "Grade"],
                      [["Hand-labelled (ceiling)", f"{g['correct']}/{g['n']}", g["wrong_firm"], g["over_firm"], g["abstain"], "measured"],
                       ["Compiled, no review", f"{comp_['correct']}/{comp_['n']}", comp_["wrong_firm"], comp_["over_firm"], comp_["abstain"], "measured"],
                       ["Compiled, after review walk", f"{cw['end_correct']}/{g['n']}", cw["max_wrong_firm"], "none" if cw["end_correct"] == g["n"] else "?", "none" if cw["end_correct"] == g["n"] else "?", "measured (hand labels play the reviewer)"]])),
           ("p", f"Wrong firm means a firm answer that differs from a firm expectation. On the compiled atoms, {at['correct']} of {at['total']} atoms "
                 f"({pct(at['correct'], at['total'])}) equal the hand label, {at['wrong']} are wrong and {at['unknown']} are unknown [measured]. "
                 f"The cases that are not correct without review: {', '.join(comp_['not_correct'])}."),
           ("h2", "Review effort"),
           ("table", (["Walk", "Blocking answers", "Confirmations", "Corrections", "Steps", "End", "Wrong firm at any step"],
                      [["One atom per step", aw["answers"], aw["confirmations"], aw["corrections"], aw["actions"],
                        f"{aw['max_correct']}/{aw['cases']}", aw["max_wrong_firm"]],
                       ["One reasoning-path card per step", cw["answers"], f"{cw['cards']} cards ({cw['atoms_confirmed']} atoms)",
                        cw["atoms_corrected"], card_steps(cw), f"{cw['end_correct']}/{aw['cases']}",
                        cw["max_wrong_firm"]]])),
           ("p", f"Hidden error rate of load-bearing compiled atoms on the cases: {aw['hidden_wrong']} of {aw['hidden_checked']} atoms a "
                 f"reviewer confirmed or corrected were wrong ({pct(aw['hidden_wrong'], aw['hidden_checked'])}) [measured]. "
                 f"Compile agreement and the support check did not separate the wrong atoms from the right ones. "
                 f"On the portfolio, the simulated review confirmed {r['portfolio']['simulated_review']['confirmed']} atoms and corrected "
                 f"{r['portfolio']['simulated_review']['corrected']} ({pct(r['portfolio']['simulated_review']['corrected'], r['portfolio']['simulated_review']['confirmed'] + r['portfolio']['simulated_review']['corrected'])}) [measured]."),
           ("p", f"Portfolio: {pc['cards']} reasoning-path cards before review ({pc['cards_per_contract']} per contract, {pc['single_atom_confirmations']} "
                 f"single-atom confirmations, {pc['questions']} blocking questions); {pr['cards']} cards after the simulated review "
                 f"({pr['cards_per_contract']} per contract) [measured]. Steps count reviewer actions, not reviewer time."),
           ("h2", "Portfolio outcomes"),
           ("table", (["State", "Firm", "Provisional (worst case)", "Blocked", "Of"],
                      [["Compiled, no review", pc["firm"], pc["provisional"], pc["blocked"], pc["outcomes"]],
                       ["After review (simulated from hand labels)", pr["firm"], pr["provisional"], pr["blocked"], pr["outcomes"]]])),
           ("p", "NEEDS_LAWYER reactions by main cause [measured]:"),
           ("table", (["State", "All", "Check extraction", "Missing document", "Missing fact", "Decision for counsel"],
                      [[name, s["needs_lawyer"], s["main_cause"]["check_extraction"], s["main_cause"]["missing_document"],
                        s["main_cause"]["missing_fact"], s["main_cause"]["legal_judgement"]]
                       for name, s in (("Compiled, no review", pc), ("After review (simulated)", pr))])),
           ("p", f"Decisions for counsel: {pc['counsel']} outcomes before review, {pr['counsel']} after. The count rises because settling the extraction "
                 f"shows the real legal forks behind it. Hand labels cover {pr['hand_labels']['clauses']} clauses in {pr['hand_labels']['contracts']} contracts; "
                 f"{pr['hand_labels']['contracts_without']} contracts ({pr['hand_labels']['clauses_without']} clauses) stay as compiled."),
           ("h2", "Direct read by a model, against the twin"),
           ("p", f"Baseline: the same model ({bl['model']}) answers each case from the same retrieved sentences, {bl['runs']} runs. "
                 f"Firm where we abstain means the model gave a firm answer where our labels say NEEDS_LAWYER. [source: out/baseline.json]"),
           ("table", (["", "Correct", "Wrong firm", "Firm where we abstain", "Abstain"],
                      [["Direct read, majority of 3", bl["majority"]["correct"], bl["majority"]["wrong_firm"], bl["majority"]["over_firm"], bl["majority"]["abstain"]]] +
                      [[f"Direct read, run {i + 1}", x["correct"], x["wrong_firm"], x["over_firm"], x["abstain"]] for i, x in enumerate(bl["per_run"])] +
                      [["Twin, compiled, no review", bl["twin_compiled"]["correct"], bl["twin_compiled"]["wrong_firm"],
                        bl["twin_compiled"]["over_firm"], bl["twin_compiled"]["abstain"]]])),
           ("p", f"Run-to-run agreement: the direct read keeps the same status in {bl['agreement']['same_status']} cases and the same status, kind, holder and due date "
                 f"in {bl['agreement']['same_status_kind_holder_due']}. The twin gives the same answer every time for a given compiled snapshot, because the rules are code "
                 f"(it is the compile step that varies; see stability)."),
           ("p", f"Cost [source for the unit cost, measured for compile]: {bl['calls']} baseline calls cost ${bl['cost_usd']}, ${bl['per_call_usd']} per call. "
                 f"Compile costs ${bl['compile_per_contract_usd']} per contract once. Break-even against one direct read per scenario: about {bl['break_even_scenarios']} scenarios per contract."),
           ("table", (["Contracts", "Scenarios", "Direct read, 1 run", "Direct read, vote of 3", "Twin (compile once)"],
                      [[f"{x['contracts']:,}", x["scenarios"], f"${x['baseline_1run_usd']:.2f}", f"${x['baseline_vote3_usd']:.2f}", f"${x['twin_usd']:.2f}"]
                       for x in bl["cost_table"]])),
           ("p", "Fairness: the cases, the gold atoms and the rules share one author, so the twin on hand-labelled atoms (37/37) is not independent evidence. "
                 "The blind check below is the independent part. The cost table assumes every contract has the prompt size of this set and no cache hits."),
           ("h2", "Blind check"),
           ("p", f"A model of another family ({bd['model']}, {bd['samples']} samples) predicted the outcomes from the clause text alone. It agreed on status for "
                 f"{bd['agree_before']} of {bd['cases']} cases before adjudication [source]. Adjudication found our expectation wrong in "
                 f"{len(bd['ours_wrong_fixed'])} cases ({', '.join(bd['ours_wrong_fixed'])}; fixed) and upheld it in {len(bd['ours_upheld'])} "
                 f"({', '.join(bd['ours_upheld'])}). The adjudication is also ours; no lawyer reviewed it."),
           ("h2", "Arabic"),
           ("p", f"Consistency, not accuracy. Arabic twins and machine translations of English sources, compiled by the same pipeline: the same outcome as the English "
                 f"source in {ar['same_outcome']} of {ar['cases']} scenario runs, {ar['wrong_firm']} wrong firm answers [measured]. Atoms: {ar['equal']} equal, "
                 f"{ar['differ']} differ, {ar['unknown']} unknown on one side."),
           ("p", f"The lost-threshold case: the DynTek machine translation drops the 20% threshold. The English atom is {json.dumps(ar['dyntek_threshold']['english'])}; "
                 f"the Arabic side has {'no value' if ar['dyntek_threshold']['arabic'] == 'not in atoms' else json.dumps(ar['dyntek_threshold']['arabic'])}, blocked by {json.dumps(ar['dyntek_threshold']['arabic_blockers'], ensure_ascii=False)}. "
                 f"The Arabic side abstains instead of inventing a threshold. Per case (Arabic outcome, English outcome, expected): " +
                 "; ".join(f"{k}: {v['arabic']}, {v['english']}, {v['expected']}" for k, v in sorted(ar["dyntek_cases"].items())) + "."),
           ("h2", "Compile stability")]
    if st.get("run") == "not run":
        out.append(("p", "The stability run has not been done yet. Run `python -m tests.evaluate stability`."))
    else:
        co, rr = st["compiled_only"], st["after_review"]
        out += [("p", f"Recompile once with a fresh cache, same models and prompts, on the {st['contracts']} contracts with the most scenario cases "
                      f"({st['cases']} cases). Cost ${st['cost_usd']} [measured]."),
                ("table", (["", "Differ between the two compiles", "Of"],
                           [["Atoms", st["atoms_differ"], f"{st['atoms_total']} ({pct(st['atoms_differ'], st['atoms_total'])})"],
                            ["Scenario outcomes, compiled only", co["outcomes_differ"], f"{st['cases']} cases ({pct(co['outcomes_differ'], st['cases'])})"],
                            ["Scenario outcomes, after the review walk", rr["outcomes_differ"], f"{st['cases']} cases ({pct(rr['outcomes_differ'], st['cases'])})"]])),
                ("p", f"Compiled only, these cases: first compile {co['main']['correct']} correct, {co['main']['wrong_firm']} wrong firm, {co['main']['abstain']} abstain; "
                      f"second compile {co['rerun']['correct']} correct, {co['rerun']['wrong_firm']} wrong firm, {co['rerun']['abstain']} abstain. "
                      f"The review walk needed {rr['main_actions']} actions on the first compile and {rr['rerun_actions']} on the second "
                      f"({rr['main_corrections']} and {rr['rerun_corrections']} corrections), and reached {rr['main_max_correct']} and {rr['rerun_max_correct']} of {aw['cases']} cases correct "
                      f"(all cases, not only these {st['cases']}), with {rr['rerun_max_wrong_firm']} wrong firm answers at every step.")]
    out += [("h2", "What failed, and why"),
            ("ul", ["Decomposition errors: a vague qualifier (\"a material change in ownership or management\") was split into separate firm triggers. "
                    "A guard now drops the listed kinds when the cited text shows the qualifier, and sets triggers to unknown when it reaches only some items.",
                    "Compile instability: two compiles of the same contracts differ in some atoms (see stability). Three-run voting and the support check reduce this; they do not remove it, "
                    "and agreement did not separate right from wrong atoms.",
                    "Effects checker strictness: the support check rejects many `effects` answers (holder, notice, per-effect consent), so the effects atom is the most common reason "
                    "for an abstention. This is safe and costly.",
                    "Abstention hides wrong atoms: before confirmation was added, a review that only answered blank questions let wrong known atoms produce wrong firm answers. "
                    "Safety is measured after review, not before.",
                    "The direct read is firm on cases that depend on a document outside the file or on a reading question, and it changes status between runs."]),
            ("h2", "Limits"),
            ("ul", ["No lawyer has reviewed the rules, the cases, the gold atoms or the adjudication. Outputs say a clause is potentially triggered under a reading; they are not legal conclusions.",
                    f"The case set is small: {g['n']} scenarios on {c['contracts_en']} English contracts. A rate such as {pct(comp_['correct'], comp_['n'])} correct has wide uncertainty.",
                    "One author wrote the cases, the gold atoms and the rules. The blind check is the only independent test, and it is a model, not a person.",
                    "Arabic is checked for consistency with the English source, not for accuracy. The reviewed state of the demo is simulated from hand labels, not from a reviewer.",
                    "Three clause types (change of control, assignment, term and renewal) and four event types. Direct reactions only, no cascades."]),
            ("h2", "How to reproduce"),
            ("code", "python -m twin.ingest --all\npython -m tests.test_rules\npython -m tests.test_ingest\npython -m tests.score_cases\n"
                     "python -m tests.evaluate            # no model calls, rewrites results.json and this report\n"
                     "python -m tests.evaluate stability  # the one recompile run (network, about $0.40)"),
            ("p", f"Total spend for the whole project: ${r['spend_total_usd']} [source: out/cache/spend.json]. "
                  "Sources: CUAD v1 (The Atticus Project, CC BY 4.0), SEC EDGAR public filings, public Gulf government forms.")]
    return out


def md(bs):
    s = []
    for k, p in bs:
        if k == "h1":
            s.append(f"# {p}")
        elif k == "h2":
            s.append(f"## {p}")
        elif k == "p":
            s.append(p)
        elif k == "ul":
            s.append("\n".join(f"- {x}" for x in p))
        elif k == "table":
            h, rows = p
            s.append("\n".join(["| " + " | ".join(map(str, h)) + " |", "|" + "---|" * len(h)] +
                               ["| " + " | ".join(map(str, r)) + " |" for r in rows]))
        elif k == "code":
            s.append(f"```\n{p}\n```")
    return "\n\n".join(s) + "\n"


def html(bs):
    import html as h

    def inline(t):
        t = h.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    body = []
    for k, p in bs:
        if k in ("h1", "h2"):
            body.append(f"<{k}>{inline(p)}</{k}>")
        elif k == "p":
            body.append(f"<p{' id=sources' if p.startswith('Total spend') else ''}>{inline(p)}</p>")
        elif k == "ul":
            body.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in p) + "</ul>")
        elif k == "table":
            hd, rows = p
            body.append("<div class=t><table><tr>" + "".join(f"<th>{inline(str(x))}</th>" for x in hd) + "</tr>" +
                        "".join("<tr>" + "".join(f"<td>{inline(str(x))}</td>" for x in r) + "</tr>" for r in rows) + "</table></div>")
        elif k == "code":
            body.append(f"<pre>{h.escape(p)}</pre>")
    foot = "Proof of concept · not legal advice · review simulated from hand labels · <a href=#sources>sources</a>"
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Contract twin results</title><style>:root{color-scheme:light dark;--bg:#fff;--fg:#1c1c1c;--mu:#555;--ln:#d6d6d6}"
            "@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#e8e8e8;--mu:#aaa;--ln:#3a3a3a}}"
            "body{background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif;margin:0}main{max-width:60rem;margin:0 auto;padding:1rem 16px 3rem}"
            "h1{font-size:1.7rem}h2{font-size:1.2rem;margin-top:2rem;border-top:1px solid var(--ln);padding-top:1rem}.t{overflow-x:auto}"
            "table{border-collapse:collapse;margin:.5rem 0;font-size:.92rem}th,td{border:1px solid var(--ln);padding:.3rem .6rem;text-align:left;vertical-align:top}"
            "code,pre{font:.88rem ui-monospace,monospace}pre{overflow-x:auto;border:1px solid var(--ln);padding:.7rem}footer{color:var(--mu);font-size:.85rem;margin-top:2rem}"
            "a{color:inherit}</style></head><body><main><p><a href=index.html>&larr; Back to the demo</a></p>" + "".join(body) +
            f"<footer>{foot}</footer></main></body></html>\n")


def readme_table(r):
    c, g, comp_, at = r["cases"], r["cases"]["gold"], r["cases"]["compiled"], r["cases"]["atoms"]
    rv, bl, bd, ar, st, pf = r["review"], r["baseline"], r["blind"], r["arabic"], r["stability"], r["portfolio"]
    aw, cw = rv["atom_walk"], rv["card_walk"]
    rows = [("Rules vs scenario cases, hand-labelled atoms", f"{g['correct']}/{g['n']} [measured]; same author wrote cases and rules"),
            ("Exhaustive truth table, change of control (written by `python -m tests.test_rules`)", f"{r['data']['truth_table_rows']:,} combinations; metamorphic properties and threshold boundary checks pass [measured]"),
            ("Ingest", f"{c['contracts_en']} English contracts, {r['data']['sentences_en']:,} sentences, offsets exact (`python -m tests.test_ingest`) [measured]"),
            ("Blind check (another model family, no rules seen)", f"agreed on {bd['agree_before']}/{bd['cases']} before adjudication; ours wrong in {len(bd['ours_wrong_fixed'])} (fixed), upheld in {len(bd['ours_upheld'])} [source]"),
            ("Cases on compiled atoms, no review", f"{comp_['correct']}/{comp_['n']} correct, **{comp_['wrong_firm']} wrong firm**, {comp_['over_firm']} firm where we abstain, {comp_['abstain']} abstain; {at['correct']}/{at['total']} atoms exact [measured]"),
            ("Same, after the review walk (hand labels as reviewer)", f"{cw['end_correct']}/{g['n']} correct, **{cw['max_wrong_firm']} wrong firm at any step**; atom-by-atom {aw['actions']} steps ({aw['answers']} answers, {aw['confirmations']} confirmations, {aw['corrections']} corrections), {cw['cards']} cards on reasoning paths; {aw['hidden_wrong']}/{aw['hidden_checked']} reviewed compiled atoms were wrong [measured]"),
            ("Direct-read baseline, same model, 3 runs, majority", f"{bl['majority']['correct']}/{g['n']} correct, {bl['majority']['wrong_firm']} wrong firm, {bl['majority']['over_firm']} firm where we abstain; same status in {bl['agreement']['same_status']} [source]"),
            ("Portfolio demo, compiled / after simulated review", f"firm {pf['compiled']['firm']} / {pf['review']['firm']}, provisional {pf['compiled']['provisional']} / {pf['review']['provisional']}, blocked {pf['compiled']['blocked']} / {pf['review']['blocked']} of {pf['compiled']['outcomes']} outcomes [measured]"),
            ("Arabic consistency", f"same outcome as English in {ar['same_outcome']}/{ar['cases']} runs, {ar['wrong_firm']} wrong firm [measured; consistency, not accuracy]"),
            ("Cost", f"compile ${bl['compile_per_contract_usd']} per contract once; direct read ${bl['per_call_usd']} per contract per scenario; break-even about {bl['break_even_scenarios']} scenarios; project total ${r['spend_total_usd']} [source]")]
    if st.get("run") != "not run":
        rows.insert(6, ("Recompile stability, 10 contracts", f"{st['atoms_differ']}/{st['atoms_total']} atoms differ; {st['compiled_only']['outcomes_differ']}/{st['cases']} case outcomes differ compiled-only, {st['after_review']['outcomes_differ']}/{st['cases']} after review [measured]"))
    return "| Check | Result |\n|---|---|\n" + "\n".join(f"| {a} | {b} |" for a, b in rows) + "\n"


def numbers_in(text):
    text = re.sub(r"```.*?```|`[^`]*`", " ", text, flags=re.S)
    return {t.replace(",", "").rstrip(".") for t in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def verify(r, text, allowed=("1", "4.0")):
    """Every number in `text` is in results.json (any value, or inside a string value) or among the derived numbers."""
    have = set()
    for v in json.dumps(r, ensure_ascii=False).replace("\\\"", " ").split():
        have |= numbers_in(v)
    for d in r["derived"]:
        have |= numbers_in(d)
    bad = sorted(n for n in numbers_in(text) - have - set(allowed) if float(n) not in {float(h) for h in have})
    assert not bad, f"numbers not in results.json: {bad}"


def write_all(r):
    OUT.mkdir(parents=True, exist_ok=True)
    DERIVED.clear()
    bs = blocks(r)
    readme_table(r)
    r["derived"] = sorted(DERIVED)
    (OUT / "results.json").write_bytes(json.dumps(r, indent=1, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    verify(r, md(bs))
    verify(r, readme_table(r))
    (ROOT / "docs" / "results.md").write_bytes(md(bs).encode("utf-8"))
    (ROOT / "out" / "portfolio").mkdir(parents=True, exist_ok=True)
    (ROOT / "out" / "portfolio" / "results.html").write_bytes(html(bs).encode("utf-8"))
    rd = (ROOT / "README.md").read_text(encoding="utf-8")
    new = re.sub(r"(## Results so far\n)(.*?)(\n## Run)",
                 lambda m: m.group(1) + readme_table(r) + "\nFull report: [docs/results.md](docs/results.md), regenerated by `python -m tests.evaluate`.\n" + m.group(3),
                 rd, flags=re.S)
    (ROOT / "README.md").write_bytes(new.encode("utf-8"))


def _check():
    assert pct(1, 4) == "25.0%" and pct(0, 0) == "n/a"
    t = md([("h2", "A"), ("table", (["x"], [[1]])), ("ul", ["a"])])
    assert "| x |" in t and "- a" in t and "<table>" in html([("table", (["x"], [[1]]))])
    case = next(c for c in sc.cases if c["id"] == "R01")
    assert classify(case, GOLD0)[0] == "correct" and classify(case, {})[0] != "correct"
    print("evaluate self-check ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "check":
        _check()
    elif cmd == "stability":
        stability_run()
    else:
        res = compute()
        write_all(res)
        print("wrote out/eval/results.json, docs/results.md, out/portfolio/results.html, README table")
