"""Ingest contract text into SQLite: numbered sentences with exact character offsets, defined terms,
and (for CUAD contracts) the sentences that CUAD's labels cover. No models, no network.

    python -m twin.ingest --all          # ingest every curated text, idempotent
    python -m twin.ingest path.txt ...   # ingest specific files
    python -m twin.ingest                # run the self-check
"""
import ast
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "twin.sqlite"
CURATED = ROOT / "data" / "curated"
CUAD_CATEGORIES = ["Change Of Control", "Anti-Assignment", "Renewal Term",
                   "Notice Period To Terminate Renewal", "Expiration Date", "Effective Date"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS contract(id TEXT PRIMARY KEY, title TEXT, source_path TEXT, sha256 TEXT, language TEXT);
CREATE TABLE IF NOT EXISTS sentence(contract_id TEXT, sid INTEGER, char_start INTEGER, char_end INTEGER, text TEXT,
                                    PRIMARY KEY(contract_id, sid));
CREATE TABLE IF NOT EXISTS defined_term(contract_id TEXT, term TEXT, sid INTEGER, pattern TEXT,
                                        PRIMARY KEY(contract_id, term, sid, pattern));
CREATE TABLE IF NOT EXISTS cuad_label(contract_id TEXT, category TEXT, sid INTEGER,
                                      PRIMARY KEY(contract_id, category, sid));
"""

# ---- sentence splitting ----------------------------------------------------------------------------------------

ABBREV = {"inc.", "co.", "ltd.", "no.", "nos.", "sec.", "u.s.", "u.s.a.", "e.g.", "i.e.", "etc.", "corp.", "l.p.",
          "l.l.c.", "mr.", "mrs.", "ms.", "dr.", "st.", "jr.", "sr.", "vs.", "art.", "s.a.", "n.v.", "p.c.", "pty."}
NOT_END_TOKEN = re.compile(r"^(?:\(?\d+(?:\.\d+)*\.|[A-Za-z]\.)$")      # "1.", "1.01.", "J."
BLANK_LINES = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")
EN_END = re.compile(r"[.?!][\"”’')\]]*\s+")
AR_END = re.compile(r"[.؟!؛][\"”»)\]]*(?:\s+|$)")
NEXT_OK = re.compile(r"[A-Z\"“‘(\[\d]")
# a heading starts on a new line, or after a run of 2+ spaces (wrapped or flattened text)
HEADING = re.compile(r"(?:\n[ \t]*|[ \t]{2,})(?=(?:ARTICLE|Article|SECTION|Section)\s+[\dIVXLC]+\b|\d+\.\d+\.?\s+[A-Z]"
                     r"|\([a-z]{1,4}\)\s)")
LEADING_PUNCT = "(\"“‘'["


def _boundaries(seg, start, arabic):
    """Offsets (absolute) at which a new sentence begins inside seg."""
    cuts = set()
    if arabic:
        for m in re.finditer(r"\n", seg):
            cuts.add(m.end())
        for m in AR_END.finditer(seg):
            i = m.start()
            if seg[i] == "." and i > 0 and seg[i - 1].isdigit() and seg[i + 1:i + 2].isdigit():
                continue
            cuts.add(m.end())
    else:
        for m in EN_END.finditer(seg):
            nxt = seg[m.end():m.end() + 1]
            if not nxt or not NEXT_OK.match(nxt):
                continue
            tok = seg[:m.start() + 1].split()[-1].lstrip(LEADING_PUNCT)
            if tok.lower() in ABBREV or NOT_END_TOKEN.match(tok):
                continue
            cuts.add(m.end())
        for m in HEADING.finditer(seg):
            cuts.add(m.end())
    return sorted(c + start for c in cuts)


def split_sentences(text, arabic=False):
    """[(char_start, char_end)] with text[s:e] stripped and non-empty. Blank lines always split."""
    spans, pos = [], 0
    segs = []
    for m in BLANK_LINES.finditer(text):
        segs.append((pos, m.start()))
        pos = m.end()
    segs.append((pos, len(text)))
    for a, b in segs:
        seg = text[a:b]
        cuts = [a] + _boundaries(seg, a, arabic) + [b]
        for s, e in zip(cuts, cuts[1:]):
            piece = text[s:e]
            lead = len(piece) - len(piece.lstrip())
            s2, e2 = s + lead, s + len(piece.rstrip())
            if e2 > s2:
                spans.append((s2, e2))
    return spans


def detect_language(text):
    letters = [c for c in text if c.isalpha()]
    ar = sum("؀" <= c <= "ۿ" for c in letters)
    return "ar" if letters and ar / len(letters) > 0.3 else "en"


# ---- defined terms ---------------------------------------------------------------------------------------------

QO, QC = "\"“«", "\"”»"
TERM = rf"([^{QO}{QC}\n]{{1,80}}?)"
DEF_EN = re.compile(rf"[{QO}]{TERM}[{QC}][\s,]*(?:shall\s+)?(?:means?\b|(?:has|have)\s+the\s+meaning)")
DEF_AR = [
    re.compile(rf"[{QO}]{TERM}[{QC}]\s*(?:ويقصد|يقصد|يعني|تعني|ويعني)"),
    re.compile(rf"(?:يقصد|يعني|ويقصد)\s+ب(?:ـ|\s)?\s*[{QO}]{TERM}[{QC}]"),
    re.compile(r"(?:يقصد|ويقصد)\s+بـ\s*([^\s:–\-،\"“«]+(?:\s+[^\s:–\-،\"“«]+){0,2}?)\s*[:–\-،]"),
]
PAREN = re.compile(r"\(([^()]{0,200})\)")
QUOTED = re.compile(rf"[{QO}]{TERM}[{QC}]")


def _ok_term(t):
    t = " ".join(t.split())
    return t if t and len(t.split()) <= 8 and not t.isdigit() else None


def find_defined_terms(sentence):
    """[(term, pattern)] defined in one sentence."""
    out = []
    for m in DEF_EN.finditer(sentence):
        out.append((m.group(1), "means"))
    for i, rx in enumerate(DEF_AR):
        for m in rx.finditer(sentence):
            out.append((m.group(1), f"ar_means{i + 1}"))
    for pm in PAREN.finditer(sentence):
        for q in QUOTED.finditer(pm.group(1)):
            out.append((q.group(1), "parenthetical"))
    seen, res = set(), []
    for t, p in out:
        t = _ok_term(t)
        if t and (t, p) not in seen:
            seen.add((t, p))
            res.append((t, p))
    return res


# ---- CUAD labels -----------------------------------------------------------------------------------------------

def label_pieces(text, labels):
    """[(category, piece, [(start, end), ...])] for each label piece; empty list = not found in the text.
    A piece found several times (a short date, say) maps to every occurrence; '<omitted>' splits a label."""
    res = []
    for cat in CUAD_CATEGORIES:
        raw = labels.get(cat)
        if not raw:
            continue
        for label in ast.literal_eval(raw) if isinstance(raw, str) else raw:
            for piece in label.split("<omitted>"):
                piece = piece.strip()
                if not piece:
                    continue
                hits = [(m.start(), m.end()) for m in re.finditer(re.escape(piece), text)]
                if not hits:       # whitespace in the label may differ from the text
                    rx = r"\s+".join(re.escape(w) for w in piece.split())
                    hits = [(m.start(), m.end()) for m in re.finditer(rx, text)]
                res.append((cat, piece, hits))
    return res


def sentences_overlapping(spans, start, end):
    return [i for i, (s, e) in enumerate(spans) if s < end and e > start]


# ---- ingest ----------------------------------------------------------------------------------------------------

def contract_id(path):
    return re.sub(r"[^a-z0-9]+", "_", Path(path).stem.lower()).strip("_")


def _cuad_entry(path):
    idx = Path(path).parent / "_index.json"
    if not idx.exists():
        return None
    for e in json.loads(idx.read_text(encoding="utf-8")):
        if e["file"] == Path(path).name:
            return e
    return None


def ingest(path, db=DB):
    """Ingest one text file; returns the contract id. Re-running replaces that contract's rows."""
    path = Path(path)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    cid = contract_id(path)
    lang = detect_language(text)
    spans = split_sentences(text, arabic=lang == "ar")
    entry = _cuad_entry(path)
    title = Path(entry["cuad_filename"]).stem if entry else path.stem
    try:
        src = str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        src = str(path)

    Path(db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        for t in ("contract", "sentence", "defined_term", "cuad_label"):
            con.execute(f"DELETE FROM {t} WHERE {'id' if t == 'contract' else 'contract_id'}=?", (cid,))
        con.execute("INSERT INTO contract VALUES(?,?,?,?,?)",
                    (cid, title, src, hashlib.sha256(raw).hexdigest(), lang))
        con.executemany("INSERT INTO sentence VALUES(?,?,?,?,?)",
                        [(cid, i, s, e, text[s:e]) for i, (s, e) in enumerate(spans)])
        terms = {(cid, t, i, p) for i, (s, e) in enumerate(spans) for t, p in find_defined_terms(text[s:e])}
        con.executemany("INSERT INTO defined_term VALUES(?,?,?,?)", sorted(terms))
        if entry:
            rows = {(cid, cat, sid) for cat, _, hits in label_pieces(text, entry["labels"])
                    for a, b in hits for sid in sentences_overlapping(spans, a, b)}
            con.executemany("INSERT INTO cuad_label VALUES(?,?,?)", sorted(rows))
        con.commit()
    finally:
        con.close()
    return cid


def all_paths():
    return [p for d in ("cuad", "edgar", "arabic") for p in sorted((CURATED / d).glob("*.txt"))]


def _selfcheck():
    t = "Acme Inc. shall pay U.S. dollars (e.g. cash). Sec. 5 applies.\n\n1.01. Term. The \"Term\" means one year; see (a) below."
    sp = split_sentences(t)
    got = [t[s:e] for s, e in sp]
    assert got == ["Acme Inc. shall pay U.S. dollars (e.g. cash).", "Sec. 5 applies.", "1.01. Term.",
                   "The \"Term\" means one year; see (a) below."], got
    ar = "يقصد بـ\"العقد\" هذا الاتفاق. هل يسري؟ نعم!\nسطر جديد ٣.٥ بالمئة"
    got = [ar[s:e] for s, e in split_sentences(ar, arabic=True)]
    assert len(got) == 4 and got[3] == "سطر جديد ٣.٥ بالمئة", got
    assert ("العقد", "ar_means2") in find_defined_terms(got[0])
    assert find_defined_terms('A “Change of Control” shall mean X (the "Company", and "Buyer").') == [
        ("Change of Control", "means"), ("Company", "parenthetical"), ("Buyer", "parenthetical")]
    print("ingest self-check ok")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        _selfcheck()
    else:
        for p in (all_paths() if args == ["--all"] else args):
            print(ingest(p))
