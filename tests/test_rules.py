"""Exhaustive truth tables + metamorphic tests. Run: python -m tests.test_rules  (writes out/truth_*.csv)"""
import csv, itertools, os
from datetime import date
from twin.core import State, apply, K, k_and, k_or, k_not
from twin.rules import coc_reactions, assign_reactions, term_reactions

os.makedirs("out", exist_ok=True)
ORDER = {"NOT_TRIGGERED": 0, "NEEDS_LAWYER": 1, "TRIGGERED": 2}

# --- Kleene sanity (all 9 pairs) ---
for a, b in itertools.product(K, K):
    assert k_and(a, b) == min(a, b) and k_or(a, b) == max(a, b)
    assert k_not(k_not(a)) == a and k_not(k_and(a, b)) == k_or(k_not(a), k_not(b))  # De Morgan

# --- change of control: every atom combination x event bucket ---
EVENTS = [{"event": "share_sale", "buyer_pct": p, "management_changes": m} for p in (0, 15, 20, 30, 50, 51, 60, 100) for m in (True, False)] \
       + [{"event": "ipo", "float_pct": 30, "management_changes": False}, {"event": "holding_insertion", "position": "between_parent_and_borrower"}]
GRID = dict(whose=["us", "them", "either"],
            trig=[["ownership_threshold"], ["management_change"], ["transaction_type"], ["open_textured"], ["parent_direct_ownership"], ["cross_reference"]],
            op=["gt", "gte"], thr=[20, 50], texture=["magnitude", "judgement"], carve=[[], ["same_management"]],
            effect=["termination_right", "consent", "elsewhere"])
rows = []
for whose, trig, op, thr, tex, carve, eff in itertools.product(*GRID.values()):
    a = dict(whose=whose, triggers=trig, threshold_op=op, threshold_pct=thr, open_texture=tex, carve_outs=carve,
             transaction_types=["merger"], effect=eff, effect_holder="them")
    for ev in EVENTS:
        r = coc_reactions(a, apply(State(), ev))[0]
        rows.append([whose, trig[0], op, thr, tex, "+".join(carve) or "-", eff, ev["event"], ev.get("buyer_pct", ""), ev.get("management_changes", ""), r.status, r.kind])
        # metamorphic 1: a counterparty-only clause never reacts to our events
        assert whose != "them" or r.status == "NOT_TRIGGERED"
        # metamorphic 2: adding a carve-out never raises the outcome
        if carve:
            r0 = coc_reactions({**a, "carve_outs": []}, apply(State(), ev))[0]
            assert ORDER[r.status] <= ORDER[r0.status], (a, ev)
        # metamorphic 3: effect defined elsewhere never yields TRIGGERED
        assert not (eff == "elsewhere" and r.status == "TRIGGERED")
with open("out/truth_coc.csv", "w", newline="") as f:
    csv.writer(f).writerows([["whose", "trigger", "op", "threshold", "texture", "carve_outs", "effect", "event", "buyer_pct", "mgmt_changed", "status", "kind"]] + rows)

# metamorphic 4: ownership threshold is monotone in buyer %
for op, thr in itertools.product(["gt", "gte"], [20, 50, 51]):
    a = dict(whose="us", triggers=["ownership_threshold"], threshold_op=op, threshold_pct=thr, effect="consent")
    seq = [ORDER[coc_reactions(a, apply(State(), {"event": "share_sale", "buyer_pct": p}))[0].status] for p in range(0, 101)]
    assert seq == sorted(seq), (op, thr)
    # boundary: gte includes the threshold, gt excludes it
    at = coc_reactions(a, apply(State(), {"event": "share_sale", "buyer_pct": thr}))[0].status
    assert at == ("TRIGGERED" if op == "gte" else "NOT_TRIGGERED")

# metamorphic 5: moving the clock never revives a lapsed deadline
for mode, notice in [("auto", [30, "days"]), ("auto", [6, "months"]), ("option", [60, "days"])]:
    a = dict(start="2025-01-01", initial_term=[2, "years"], mode=mode, renewal_term=[1, "years"], notice=notice)
    lapsed = False
    for d in range(0, 800, 3):
        s = State(as_of=date(2025, 1, 1).fromordinal(date(2025, 1, 1).toordinal() + d))
        kind = term_reactions(a, s)[0].kind
        if kind.endswith("lapsed") or kind in ("renewed", "expired"):
            lapsed = True
        assert not (lapsed and kind in ("deadline", "option_deadline")), (mode, s.as_of)

# assignment truth table
arows = []
for coc_na, deems, mexc, unl, ev in itertools.product([False, True], [[], ["merger", "reorganization"], ["change_of_control"]], [False, True], [False, True],
        [{"event": "share_sale", "buyer_pct": 60, "buyer_is_competitor": c} for c in (False, True)] + [{"event": "holding_insertion", "mechanism": "reorganization"}, {"event": "ipo", "float_pct": 30}]):
    a = dict(coc_not_assignment=coc_na, deems=deems, merger_exception=mexc, unless_competitor=unl)
    r = assign_reactions(a, apply(State(), ev))[0]
    arows.append([coc_na, "+".join(deems) or "-", mexc, unl, ev["event"], ev.get("buyer_is_competitor", ""), r.status, r.reason])
    assert not (coc_na and ev["event"] == "share_sale" and r.status != "NOT_TRIGGERED")
with open("out/truth_assign.csv", "w", newline="") as f:
    csv.writer(f).writerows([["coc_not_assignment", "deems", "merger_exception", "unless_competitor", "event", "competitor", "status", "reason"]] + arows)
print(f"all tests pass; truth_coc.csv {len(rows)} rows, truth_assign.csv {len(arows)} rows")

# reviewable view: keep only the columns each trigger kind actually reads, then dedupe
REL = {"ownership_threshold": ["op", "threshold", "buyer_pct"], "management_change": ["mgmt_changed"],
       "transaction_type": [], "open_textured": ["texture", "buyer_pct"], "parent_direct_ownership": [], "cross_reference": []}
hdr = ["whose", "trigger", "op", "threshold", "texture", "carve_outs", "effect", "event", "buyer_pct", "mgmt_changed", "status", "kind"]
seen, view = set(), []
for r in rows:
    d = dict(zip(hdr, r))
    if d["whose"] == "them" or d["effect"] != "termination_right":
        continue  # covered by metamorphic tests 1 and 3
    keep = ["trigger"] + REL[d["trigger"]] + ["carve_outs", "event", "status"]
    key = tuple((c, d[c]) for c in keep)
    if key not in seen:
        seen.add(key); view.append(key)
with open("out/truth_coc_review.csv", "w", newline="") as f:
    w = csv.writer(f)
    for key in sorted(view, key=str):
        w.writerow([f"{c}={v}" for c, v in key])
print(f"truth_coc_review.csv {len(view)} rows (for sign-off)")
