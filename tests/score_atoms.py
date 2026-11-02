"""Compare compiled atoms with the hand-labelled gold atoms (exact match per key), or two compiled runs with each other.
Run from repo root: python -m tests.score_atoms [out/compiled_atoms.json] [--vs out/compiled_atoms_run2.json] [-v]"""
import json
import sys
from collections import Counter, defaultdict


def norm(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else float(v)
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return {k: norm(x) for k, x in sorted(v.items())}
    items = [norm(x) for x in v]
    return sorted(items, key=json.dumps) if all(isinstance(x, (str, dict)) for x in items) else items


def load(p):
    return json.load(open(p, encoding="utf-8"))


SETS = {"triggers", "carve_outs", "transaction_types", "deems", "effects"}


def flat(fam):
    """Family -> {key: value} with effects split into one set of kinds plus per-kind holder, notice and consent flag."""
    out = {k: v for k, v in fam.items() if not k.startswith("_") and k not in ("retained_start_pct", "effect", "effect_holder", "notice", "effects", "unless_consented")}
    effs = fam.get("effects") or ([{k: fam[k] for k in ("effect", "effect_holder", "notice", "unless_consented") if k in fam}] if "effect" in fam else [])
    if effs:
        out["effects"] = [e["effect"] for e in effs]
        for e in effs:
            out["effect_holder:" + e["effect"]] = e.get("effect_holder")
            out["effect_notice:" + e["effect"]] = e.get("notice")
            out["unless_consented:" + e["effect"]] = bool(e.get("unless_consented"))
    elif "notice" in fam:
        out["notice"] = fam["notice"]
    return out


def vs_gold(comp, gold, verbose):
    tot, stats, wrong = Counter(), defaultdict(Counter), []
    sets = defaultdict(lambda: [0, 0, 0, 0])      # key -> [exact, hit, claimed, wanted]
    for src, fams in gold.items():
        if src.startswith("_"):
            continue
        for fam, atoms in fams.items():
            c = comp.get(src, {}).get(fam)
            cf, gf = flat(c) if c else {}, flat(atoms)
            for key, gv in gf.items():
                name = key.split(":")[0]
                if c is None:
                    res = "missing"
                elif key not in cf:
                    res = "unknown" if "_unknown" in c or "_partial" in c else "missing"
                    if name == "effects" and "_unknown" not in c:
                        res = "missing"
                else:
                    res = "correct" if norm(cf[key]) == norm(gv) else "wrong"
                    if res == "wrong":
                        wrong.append((src, fam, key, gv, cf[key]))
                if name in SETS and key == name:
                    row = sets[(fam, name)]
                    row[3] += len(gv)
                    if key in cf:
                        row[0] += res == "correct"
                        row[1] += len(set(gv) & set(cf[key]))
                        row[2] += len(set(cf[key]))
                stats[(fam, name)][res] += 1
                stats[(fam, "ALL")][res] += 1
                tot[res] += 1
    for (fam, key), c in sorted(stats.items(), key=lambda x: (x[0][0], x[0][1] != "ALL", x[0][1])):
        n = sum(c.values())
        pr = ""
        if (fam, key) in sets:
            ex, hit, claimed, wanted = sets[(fam, key)]
            pr = f"  set: exact {ex}, precision {hit}/{claimed}, recall {hit}/{wanted}"
        print(f"{fam:6} {key:22} {c['correct']:3}/{n:<3} correct  wrong {c['wrong']}  unknown {c['unknown']}  missing {c['missing']}{pr}")
    n = sum(tot.values())
    print(f"TOTAL {tot['correct']}/{n} correct ({tot['correct'] / n:.0%}), wrong {tot['wrong']}, unknown {tot['unknown']}, missing {tot['missing']}")
    if verbose:
        for w in wrong:
            print("WRONG", *w)


def vs_run(a, b):
    diff = tot = 0
    for src in sorted(set(a) | set(b)):
        for fam in sorted(set(a.get(src, {})) | set(b.get(src, {}))):
            x, y = a.get(src, {}).get(fam), b.get(src, {}).get(fam)
            keys = set(x or {}) | set(y or {})
            for k in keys or {"<family>"}:
                tot += 1
                same = norm((x or {}).get(k, "<none>")) == norm((y or {}).get(k, "<none>"))
                diff += not same
    print(f"atoms differing between runs: {diff}/{tot} ({diff / max(tot, 1):.1%})")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    main = args[0] if args else "out/compiled_atoms.json"
    if "--vs" in sys.argv:
        vs_run(load(main), load(sys.argv[sys.argv.index("--vs") + 1]))
    else:
        vs_gold(load(main), load("cases/gold_atoms.json"), "-v" in sys.argv)
