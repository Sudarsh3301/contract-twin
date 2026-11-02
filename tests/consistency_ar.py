"""Cross-lingual consistency of compiled atoms: Arabic twins and translations vs their English source.
Run from repo root: python -m tests.consistency_ar  (reads out/compiled_atoms.json, out/compiled_atoms_ar.json)."""
import json
import sqlite3
from tests import score_cases as sc

UNK = "UNKNOWN"
gold_ar = json.load(open("cases/gold_atoms_ar.json", encoding="utf-8"))
en = json.load(open("out/compiled_atoms.json", encoding="utf-8"))
ar = json.load(open("out/compiled_atoms_ar.json", encoding="utf-8"))
cases = sc.cases


def flat(fam):
    """Family -> {atom: value}; atoms named as blockers are UNKNOWN; one top-level effect becomes an effects list."""
    if not fam or fam.get("present") is False:
        return {}
    out = {k: v for k, v in fam.items() if not k.startswith("_") and k not in ("effect", "effect_holder", "notice", "unless_consented")}
    if "effect" in fam:
        out["effects"] = [{k: fam[k] for k in ("effect", "effect_holder", "notice", "unless_consented") if k in fam}]
    if "effects" in out:
        out["effects"] = sorted(out["effects"], key=lambda e: e["effect"])
    for b in fam.get("_unknown", []):
        k = b.split(" is ")[0]
        out["effects" if k == "unless_consented" else k] = UNK
    for k in ("triggers", "transaction_types", "carve_outs", "deems"):
        if isinstance(out.get(k), list):
            out[k] = sorted(out[k])
    return out


def compare(a, e):
    """Per atom: equal / differ / unknown (either side UNKNOWN). Absent on one side and present on the other = differ."""
    rows = []
    for fam in ("coc", "assign", "term"):
        fa, fe = flat(a.get(fam)), flat(e.get(fam))
        for k in sorted(set(fa) | set(fe)):
            va, ve = fa.get(k), fe.get(k)
            cls = "unknown" if UNK in (va, ve) else "equal" if va == ve else "differ"
            rows.append((f"{fam}.{k}", cls, va, ve))
    return rows


def outcome(atoms, case):
    sc.gold = {case["src"].split(" ")[0]: atoms}
    st, rs = sc.run(case)
    errs = sc.check(case, st, rs)
    want = case["expected"]["status"]
    tag = "correct" if not errs else "wrong firm" if st not in ("NEEDS_LAWYER", want) else \
        "abstain" if st == "NEEDS_LAWYER" else "other error"
    return st, tag, sorted((r.status, r.kind, r.holder, str(r.due)) for r in rs)


def main():
    tot = {"wrong_firm_ar": 0, "cases": 0, "same_outcome": 0}
    for key, g in gold_ar.items():
        src = g.get("_twin_of") or g.get("_translation_of") if isinstance(g, dict) else None
        if not src:
            continue
        print(f"\n=== {key} vs {src}")
        rows = compare(ar.get(key, {}), en.get(src, {}))
        gf = {f"{f}.{k}": v for f in ("coc", "assign", "term") for k, v in flat(g.get(f)).items()}
        for name, cls, va, ve in rows:
            mark = "" if va == UNK else (" ar=gold" if gf.get(name) == va else f" gold={json.dumps(gf.get(name), ensure_ascii=False)}")
            print(f"  {cls:8} {name:28} ar={json.dumps(va, ensure_ascii=False)}  en={json.dumps(ve)}{mark}")
        n = {c: sum(r[1] == c for r in rows) for c in ("equal", "differ", "unknown")}
        print("  atoms:", n)
        for c in [c for c in cases if c["src"].split(" ")[0] == src]:
            sa, ta, ra = outcome(ar.get(key, {}), c)
            se, te, re_ = outcome(en.get(src, {}), c)
            same = ra == re_
            tot["cases"] += 1
            tot["same_outcome"] += same
            tot["wrong_firm_ar"] += ta == "wrong firm"
            print(f"  case {c['id']:4} expected {c['expected']['status']:13} ar {sa:13} ({ta})  en {se:13} ({te})  {'same' if same else 'DIFFERENT'}")
    print("\ntotals:", tot)

    con = sqlite3.connect("data/twin.sqlite")
    print("\n=== known atoms in curated forms, with cited text")
    for cid in ("ejari_unified_tenancy_contract", "balady_urban_roads_contract", "balady_engineering_services_contract"):
        rows = con.execute("SELECT template, key, value, sids FROM atom WHERE run='main_ar' AND contract_id=? AND status='known' "
                           "ORDER BY template, key", (cid,)).fetchall()
        print(f"-- {cid}: {len(rows)} known")
        for t, k, v, sids in rows:
            txt = " | ".join(con.execute("SELECT text FROM sentence WHERE contract_id=? AND sid=?", (cid, s)).fetchone()[0][:160]
                             for s in json.loads(sids))
            print(f"   {t}.{k} = {v}  sids {sids}: {txt}")


if __name__ == "__main__":
    main()
