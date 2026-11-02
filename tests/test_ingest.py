"""Checks for twin.ingest. Run from repo root: python -m tests.test_ingest"""
import json
import sqlite3
import tempfile
from pathlib import Path

from twin import ingest as ing

db = Path(tempfile.mkdtemp()) / "t.sqlite"
paths = [p for p in ing.all_paths() if "arabic" not in str(p)]
ids = [ing.ingest(p, db) for p in paths]
assert len(paths) == 30 and len(set(ids)) == 30, (len(paths), len(set(ids)))
con = sqlite3.connect(db)


def dump():
    return [con.execute(f"SELECT * FROM {t} ORDER BY 1,2,3").fetchall()
            for t in ("contract", "sentence", "defined_term", "cuad_label")]


# offsets are exact for every sentence
n = 0
for cid, src in con.execute("SELECT id, source_path FROM contract"):
    raw = (ing.ROOT / src).read_bytes().decode("utf-8")
    for sid, a, b, t in con.execute("SELECT sid,char_start,char_end,text FROM sentence WHERE contract_id=?", (cid,)):
        assert raw[a:b] == t and t.strip() == t and t, (cid, sid)
        n += 1
    sids = [r[0] for r in con.execute("SELECT sid FROM sentence WHERE contract_id=? ORDER BY sid", (cid,))]
    assert sids == list(range(len(sids))), cid
print(f"offsets exact for {n} sentences")

# running twice gives the same rows
before = dump()
for p in paths:
    ing.ingest(p, db)
assert dump() == before
print("idempotent")


def has(contract_key, term):
    return con.execute("SELECT 1 FROM defined_term WHERE contract_id LIKE ? AND lower(term)=lower(?)",
                       (f"%{contract_key}%", term)).fetchone() is not None


required = [("ipayment", "Business Day"), ("ipayment", "Change of Control"), ("zounds", "Change of Control"),
            ("ediets", "Change in Control"), ("amscan", "Qualifying IPO"), ("amscan", "Change of Control")]
missed = [r for r in required if not has(*r)]
assert not missed, missed
print("required defined terms found:", len(required))

# at least 95% of CUAD label pieces land on a sentence
idx = json.loads((ing.CURATED / "cuad" / "_index.json").read_text(encoding="utf-8"))
tot = hit = 0
for e in idx:
    text = (ing.CURATED / "cuad" / e["file"]).read_bytes().decode("utf-8")
    spans = ing.split_sentences(text)
    for cat, piece, hits in ing.label_pieces(text, e["labels"]):
        tot += 1
        hit += any(ing.sentences_overlapping(spans, a, b) for a, b in hits)
assert hit / tot >= 0.95, (hit, tot)
print(f"label pieces mapped: {hit}/{tot}")
print("all tests pass")
