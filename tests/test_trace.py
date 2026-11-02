"""Reasoning traces. For every reaction on the scenario cases (gold and compiled atoms): the trace ends in one outcome
step that states the reaction, steps come in evaluation order, and every atom node cites sentences that are the exact
text of the contract at their offsets. For every outcome open on readings: one fork step per tried atom, and the branches
are the outcomes readings() gives. Run: python -m tests.test_trace"""
import json
import sqlite3
from twin import review
from twin.core import State, apply
from twin.ingest import DB, ROOT
from twin.rules import VALUE, react, readings, ref
from tests import score_cases as scs


def check_trace(r) -> None:
    assert r.trace, f"no trace: {r}"
    out = r.trace[-1]
    assert out.kind == "outcome" and sum(s.kind == "outcome" for s in r.trace) == 1, [s.id for s in r.trace]
    i = dict(out.inputs)
    assert (i["status"], i["kind"], i["holder"], i["due"], i["cause"]) == \
        (r.status, r.kind, r.holder, str(r.due) if r.due else "", r.cause), (i, r)
    assert out.value == VALUE[r.status]
    seen = set()
    for s in r.trace:
        assert s.id not in seen, f"step twice: {s.id}"
        assert all(p in seen for p in s.parents), f"{s.id} uses a later step: {s.parents}"
        seen.add(s.id)


def atom_refs(r) -> set:
    return {a for s in r.trace if s.kind in ("atom", "fork") for a in s.atom_refs}


def check_forks(t, fam, s, rs) -> int:
    """An outcome left open because readings differ: its fork steps name exactly the atoms readings() varies, and each
    branch's trace shows the assumed values of the first alternative that leads to it."""
    forks = {a for r in rs for st in r.trace if st.kind == "fork" for a in st.atom_refs}
    rds = readings(t, fam, s)
    if rds is None or len(rds) < 2:
        return 0
    varied = {ref(k) for rd in rds for w in rd["when"] for k in w["values"]}
    assert forks == varied, (forks, varied)
    for rd in rds:
        first = {ref(k): v for k, v in rd["when"][0]["values"].items()}
        for r in rd["reactions"]:
            check_trace(r)
            got = {st.atom_refs[0]: dict(st.inputs).get("assumed") for st in r.trace if st.kind == "fork" and st.atom_refs}
            assert got == first, (got, first)
            if rd["when"][0]["reading"]:
                assert any(st.id == "fork:reading" and st.label == rd["when"][0]["reading"] for st in r.trace)
    return 1


def main():
    con = sqlite3.connect(DB)
    cids = {review.contract_key(cid, lang): (cid, path) for cid, lang, path in con.execute("SELECT id, language, source_path FROM contract")}
    spans = {(c, sid): (a, b, t) for c, sid, a, b, t in con.execute("SELECT contract_id, sid, char_start, char_end, text FROM sentence")}
    src = {}
    gold = scs.gold
    compiled = json.load(open("out/compiled_atoms.json", encoding="utf-8"))
    n = dict(reactions=0, atom_nodes=0, quotes=0, forks=0)
    for name, atoms in (("gold", gold), ("compiled", compiled)):
        for case in scs.cases:
            key = case["src"].split(" ")[0]
            key = "edgar:Amscan" if key.startswith("edgar:Amscan") else key
            s = State()
            for ev in case["scenario"]:
                s = apply(s, ev)
            for t in (["term"] if case["id"].startswith("N") else ["coc", "assign"]):
                fam = atoms.get(key, {}).get(t)
                if fam is None:
                    continue
                rs = react(t, fam, s)
                for r in rs:
                    check_trace(r)
                    n["reactions"] += 1
                if any(r.status == "NEEDS_LAWYER" for r in rs) and any(st.kind == "fork" for r in rs for st in r.trace):
                    n["forks"] += check_forks(t, fam, s, rs)
                cid, path = cids[key]
                text = src.setdefault(cid, (ROOT / path).read_bytes().decode("utf-8"))
                for a in sorted(set().union(*(atom_refs(r) for r in rs))):
                    n["atom_nodes"] += 1
                    for sid, quote in review.cited_text(con, cid, t, a, fam, "gold" if name == "gold" else "main"):
                        lo, hi, stored = spans[(cid, sid)]
                        assert text[lo:hi] == quote == stored, f"{key} {t} {a} [{sid}] not verbatim"
                        n["quotes"] += 1
    assert n["forks"] > 0, "no fork was checked"
    print(f"trace tests pass: {n['reactions']} reactions, {n['atom_nodes']} atom nodes with {n['quotes']} verbatim quotes, "
          f"{n['forks']} forked outcomes match readings()")


if __name__ == "__main__":
    main()
