"""Compile contracts into typed atoms with an open model. The model only answers fixed questions; code checks every
answer (cited sentences, numbers, dates), a second model checks support, and weak evidence becomes UNKNOWN.

    python -m twin.compile --dry                 # token and cost estimate, no calls
    python -m twin.compile --only ipayment       # contracts whose id starts with the text
    python -m twin.compile [--run run2]          # all contracts; a new --run name uses a fresh cache
    python -m twin.compile --check               # offline self-check
Writes the atom table in data/twin.sqlite and out/compiled_atoms[_<run>].json (same shape as cases/gold_atoms.json).
"""
import argparse
import json
import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import llm
from .ingest import DB, ROOT

PROMPT_VERSION = "v1"
K_RUNS, TEMP, CAP, DEFS, MAXLEN = 3, 0.7, 40, 8, 1200
STATUSES = ["known", "silent", "unknown", "refers_to_missing"]

# ---- atom definitions: key -> (kind, allowed values, question) -----------------------------------------------------
YN = ["yes", "no"]
EFFECTS = ["termination_right", "automatic_termination", "consent", "notice", "prepayment", "service_limitation",
           "elsewhere"]
COC = {
    "present": ("enum", YN, "Does the contract contain a change-of-control (or change in ownership/control) provision?"),
    "whose": ("enum", ["us", "them", "either"], "Whose change of control is covered: our party, the counterparty, or either?"),
    "triggers": ("set", ["ownership_threshold", "retained_stake", "management_change", "transaction_type", "open_textured",
                         "parent_direct_ownership", "cross_reference", "undefined"],
                 "What counts as a change of control: a percentage acquired (ownership_threshold); owners keeping less than a "
                 "percentage (retained_stake); change of management/board (management_change); a type of deal such as merger "
                 "or asset sale regardless of percentage (transaction_type); vague words like significant/material/unacceptable "
                 "(open_textured); parent stops owning the borrower directly (parent_direct_ownership); defined in another "
                 "document (cross_reference); the term is used but no definition can be found (undefined)."),
    "threshold_pct": ("num", None, "Ownership percentage that triggers it (ownership_threshold)."),
    "threshold_op": ("enum", ["gt", "gte"], "'more than X%' = gt; 'X% or more' / 'at least' = gte."),
    "retained_holder": ("enum", ["sponsors", "original_owners", "principal_owners", "none"], "Whose retained stake is tested (retained_stake)."),
    "retained_pct": ("num", None, "Percentage the retained holder must keep."),
    "transaction_types": ("set", ["merger", "consolidation", "asset_sale", "reorganization", "share_sale"], "Deal types that count (transaction_type)."),
    "qipo_test": ("enum", ["proceeds", "float", "exchange", "other"], "How a Qualifying IPO is defined, if the text has one."),
    "qipo_threshold": ("num", None, "Qualifying IPO minimum proceeds in dollars (e.g. 150000000)."),
    "post_ipo_threshold_pct": ("num", None, "After a Qualifying IPO, percentage held by one person that counts as a change of control."),
    "post_ipo_relative_to_retained": ("enum", YN, "After a Qualifying IPO the holder must also hold at least as much as the retained holder."),
    "carve_outs": ("set", ["same_management", "qualifying_ipo", "prior_holders_keep_50", "consented", "non_competitor"],
                   "Exceptions: same management team stays; Qualifying IPO; prior holders keep >= 50% of the successor; "
                   "counterparty consented in writing; acquirer is not a competitor."),
    "open_texture": ("enum", ["judgement", "magnitude", "other"], "For open_textured: counterparty's judgement/opinion standard, or a magnitude word (significant/material)."),
    "effects": ("effects", None, "What the clause does on a change of control. One entry per effect. notice_n/notice_unit = days or "
                "months (days, months, business_days) within which a notice or termination must be given or takes effect."),
    "definition_location": ("enum", ["in_clause", "elsewhere_in_contract", "elsewhere_not_located", "other_document"], "Where the change-of-control definition is."),
}
ASSIGN = {
    "present": ("enum", YN, "Does the contract restrict assignment or transfer of the agreement?"),
    "restricted": ("enum", YN, "Is assignment restricted?"),
    "consent_needed": ("enum", YN, "Is the other party's consent needed to assign?"),
    "deems": ("set", ["merger", "consolidation", "reorganization", "operation_of_law", "change_of_control"],
              "Events the clause says are treated as an assignment."),
    "coc_not_assignment": ("enum", YN, "Does the contract say a change of control is NOT an assignment?"),
    "merger_exception": ("enum", YN, "Can the party assign without consent in a merger or sale of the business?"),
    "unless_competitor": ("enum", YN, "Is that exception lost if the successor is a competitor?"),
}
UNITS = ["days", "months", "years", "redacted"]
TERM = {
    "present": ("enum", YN, "Does the contract state a term, expiry or renewal mechanism?"),
    "start": ("str", None, "Start date as YYYY-MM-DD, or 'on_execution' if it starts on signing with no date stated."),
    "end_date": ("str", None, "Fixed end date as YYYY-MM-DD, if the text states one."),
    "initial_term_n": ("num", None, "Length of the initial term (number)."),
    "initial_term_unit": ("enum", UNITS, "Unit of the initial term; 'redacted' if the length is blanked out."),
    "mode": ("enum", ["auto", "evergreen", "option", "mutual", "conditional", "none"],
             "auto = renews automatically unless notice; evergreen = continues until a party gives notice; option = a party may "
             "extend; mutual = only by agreement; conditional = depends on facts outside the text; none."),
    "option_holder": ("enum", ["us", "them", "either"], "Who may extend (mode option)."),
    "renewal_term_n": ("num", None, "Length of each renewal (number)."),
    "renewal_term_unit": ("enum", UNITS, "Unit of each renewal."),
    "notice_n": ("num", None, "Notice period to stop renewal or exit (number)."),
    "notice_unit": ("enum", ["days", "months", "business_days", "missing_unit"], "Unit of that notice; 'missing_unit' if the number has no unit."),
}
SPEC = {"coc": COC, "assign": ASSIGN, "term": TERM}
INTRO = {"coc": "change of control", "assign": "assignment", "term": "term and renewal"}

# keyword -> weight; a sentence's score is the sum over distinct keywords it contains
KEYWORDS = {
    "coc": {r"change\s+(of|in)\s+(control|ownership)": 5, r"control": 2, r"merg|consolidat": 2, r"acqui": 2, r"ownership": 2,
            r"voting": 1, r"beneficial": 1, r"sale\s+of\s+(all|substantially)": 2, r"reorganiz": 1, r"shares|equity|stock": 1,
            r"assign|transfer": 1, r"تغيير\s+السيطرة|السيطرة|اندماج|استحواذ|ملكية": 3},
    "assign": {r"assign": 5, r"transfer": 2, r"operation\s+of\s+law": 4, r"delegat": 2, r"successor": 1, r"merg|consolidat": 1,
               r"sublicen": 1, r"consent": 1, r"تنازل|إحالة|نقل\s+الحقوق": 3},
    "term": {r"renew": 5, r"expir": 4, r"initial\s+term": 5, r"\bterm\b": 1, r"extend|extension": 2, r"terminat": 1, r"notice": 1,
             r"effective\s+date": 2, r"commenc": 1, r"\byears?\b|\bmonths?\b": 1, r"تجديد|انتهاء|مدة|سريان": 3},
}
# strong words: a hit anywhere in the contract means "silent" cannot be trusted
GUARD = {
    "coc": r"change\s+(of|in)\s+(control|ownership)|controlling\s+interest|sale\s+of\s+(all|substantially)|voting\s+(stock|securities|shares)|(merger|consolidation)\s+(of|with)|تغيير\s+السيطرة",
    "assign": r"assign|تنازل",
    "term": r"renew|expir|initial\s+term|تجديد|انتهاء",
}


def schema(template):
    def vt(kind, vals):
        if kind == "enum":
            return {"type": ["string", "null"], "enum": vals + [None]}
        if kind == "num":
            return {"type": ["number", "null"]}
        if kind == "str":
            return {"type": ["string", "null"]}
        if kind == "set":
            return {"type": "array", "items": {"type": "string", "enum": vals}}
        eff = {"effect": {"type": "string", "enum": EFFECTS}, "effect_holder": {"type": ["string", "null"], "enum": ["us", "them", "either", None]},
               "notice_n": {"type": ["number", "null"]}, "notice_unit": {"type": ["string", "null"], "enum": ["days", "months", "business_days", None]}}
        return {"type": "array", "items": {"type": "object", "properties": eff, "required": list(eff), "additionalProperties": False}}
    props = {k: {"type": "object", "properties": {"status": {"type": "string", "enum": STATUSES},
                                                 "sids": {"type": "array", "items": {"type": "integer"}},
                                                 "value": vt(kind, vals)},
                 "required": ["status", "sids", "value"], "additionalProperties": False}
             for k, (kind, vals, _) in SPEC[template].items()}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def system_prompt(template):
    lines = "\n".join(f"- {k} ({kind}{': ' + '/'.join(vals) if vals else ''}): {q}" for k, (kind, vals, q) in SPEC[template].items())
    return (f"You extract facts about the {INTRO[template]} provisions of ONE contract from numbered sentences. "
            "You do not decide legal outcomes. For every atom return status, sids and value.\n"
            "status: known = the cited sentences state it; silent = the sentences you were given say nothing about it; "
            "unknown = the text is unclear, conflicting or you are not sure; refers_to_missing = it depends on a document "
            "or section that is not in the sentences.\n"
            "sids: ids of the sentences that state the value (at least one when status is known; empty otherwise). "
            "Use only ids that appear in brackets. value: copy numbers and dates from the text; when status is not known "
            "use null (or [] for sets). Never guess. If two readings are possible, answer unknown.\n"
            f"Atoms:\n{lines}")


def user_prompt(title, sents):
    body = "\n".join(f"[{sid}] {t[:MAXLEN]}" for sid, t in sents)
    return (f"Contract: {title}\n'us' = the company named in the contract title above (the party whose portfolio this is); "
            f"'them' = the other party.\n\nSentences:\n{body}")


# ---- retrieval -----------------------------------------------------------------------------------------------------

def retrieve(con, cid, template):
    rows = con.execute("SELECT sid, text FROM sentence WHERE contract_id=? ORDER BY sid", (cid,)).fetchall()
    kw = [(re.compile(p, re.I), w) for p, w in KEYWORDS[template].items()]
    scored = [(sum(w for rx, w in kw if rx.search(t)), sid) for sid, t in rows]
    top = sorted([x for x in scored if x[0] > 0], key=lambda x: (-x[0], x[1]))[:CAP - DEFS]
    chosen = {sid for _, sid in top}
    text = dict(rows)
    uses = Counter()
    for sid in chosen:
        for term, dsid in con.execute("SELECT term, sid FROM defined_term WHERE contract_id=?", (cid,)):
            if dsid not in chosen and len(term) >= 4 and term in text[sid]:
                uses[dsid] += 1
    chosen |= {sid for sid, _ in uses.most_common(DEFS)}
    return [(sid, text[sid]) for sid in sorted(chosen)]


# ---- number and date checks ------------------------------------------------------------------------------------------
WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                    "fifteen sixteen seventeen eighteen nineteen".split())}
WORDS.update({w: 20 + 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())})
SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def numbers(text):
    """All numbers the text states: digits (with commas, million/billion) and spelled-out numbers."""
    out = set()
    low = text.lower()
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)(?:\s*(thousand|million|billion))?", low):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        out.add(v)
        if m.group(2):
            out.add(v * SCALE[m.group(2)])
    toks = re.findall(r"[a-z]+", low)
    i = 0
    while i < len(toks):
        if toks[i] in WORDS or toks[i] == "hundred":
            total, cur, j = 0, 0, i
            while j < len(toks) and (toks[j] in WORDS or toks[j] in ("hundred", "and") or toks[j] in SCALE):
                t = toks[j]
                if t in WORDS:
                    cur += WORDS[t]
                elif t == "hundred":
                    cur = max(cur, 1) * 100
                elif t in SCALE:
                    total += max(cur, 1) * SCALE[t]
                    cur = 0
                j += 1
            out.add(float(total + cur))
            i = j
        else:
            i += 1
    return out


def dates(text):
    """All dates the text states, as ISO strings."""
    out = set()
    mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    low = text.lower()
    for m in re.finditer(mon + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", low):
        out.add(f"{m.group(3)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}")
    for m in re.finditer(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?" + mon + r",?\s+(\d{4})", low):
        out.add(f"{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}")
    for m in re.finditer(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", low):
        y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        for mo, d in ((m.group(1), m.group(2)), (m.group(2), m.group(1))):
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                out.add(f"{y}-{int(mo):02d}-{int(d):02d}")
    out.update(re.findall(r"\d{4}-\d\d-\d\d", low))
    return out


UNIT_STEM = {"days": "day", "months": "month", "years": "year", "business_days": "business"}
NUM_KEYS = {"threshold_pct", "retained_pct", "qipo_threshold", "post_ipo_threshold_pct", "initial_term_n", "renewal_term_n", "notice_n"}


def value_matches_text(key, value, text):
    """Code-side check of a model answer against the cited text. True when nothing contradicts it."""
    low = text.lower()
    if key in NUM_KEYS:
        return value is not None and float(value) in numbers(text)
    if key in ("start", "end_date"):
        return value == "on_execution" or value in dates(text)
    if key.endswith("_unit"):
        return value in (None, "redacted", "missing_unit") or UNIT_STEM[value] in low
    if key == "effects":
        for e in value:
            if e["notice_n"] is not None and float(e["notice_n"]) not in numbers(text):
                return False
            if e["notice_unit"] and UNIT_STEM[e["notice_unit"]] not in low:
                return False
    return True


# ---- per-run validation and voting ---------------------------------------------------------------------------------

def canon(v):
    return json.dumps(sorted(v, key=json.dumps) if isinstance(v, list) else v, sort_keys=True)


def clean_run(template, raw, cand):
    """Validate one model answer: sids exist and are candidates, known atoms are cited, numbers match the text."""
    out = {}
    for key, (kind, _, _) in SPEC[template].items():
        a = raw[key]
        st, sids, val = a["status"], [s for s in a["sids"] if s in cand], a["value"]
        if st == "known":
            if val in (None, []) or (kind == "set" and not val):
                st = "silent"
            elif not sids or len(sids) != len(a["sids"]):
                st = "unknown"
            elif not value_matches_text(key, val, " ".join(cand[s] for s in sids)):
                st = "unknown"
        if st != "known":
            val, sids = None, []
        out[key] = {"status": st, "value": val, "sids": sids}
    return out


def vote(template, runs):
    """Majority over the k runs, per atom: >= 2 identical (status, value) wins, else unknown."""
    out = {}
    for key in SPEC[template]:
        cnt = Counter((r[key]["status"], canon(r[key]["value"])) for r in runs)
        (st, cv), n = cnt.most_common(1)[0]
        if n < 2:
            st, cv, n = "unknown", "null", 0
        agree = [r[key] for r in runs if (r[key]["status"], canon(r[key]["value"])) == (st, cv)]
        out[key] = {"status": st, "value": json.loads(cv) if st == "known" else None, "k_agree": n,
                    "sids": sorted({s for a in agree for s in a["sids"]})}
    return out


# ---- support check ---------------------------------------------------------------------------------------------------
SUPPORT_SCHEMA = {"type": "object", "properties": {"checks": {"type": "array", "items": {
    "type": "object", "properties": {"key": {"type": "string"}, "supported": {"type": "string", "enum": YN}},
    "required": ["key", "supported"], "additionalProperties": False}}}, "required": ["checks"], "additionalProperties": False}


def support_check(template, atoms, text, **kw):
    items = [(k, a) for k, a in atoms.items() if a["status"] == "known"]
    if not items:
        return {}, []
    blocks = "\n\n".join(f"Check {k}\nQuestion: {SPEC[template][k][2]}\nClaimed answer: {json.dumps(a['value'])}\n"
                         f"Text:\n" + "\n".join(f"- {text[s][:MAXLEN]}" for s in a["sids"]) for k, a in items)
    r = llm.ask(llm.CHECKER, "You verify claims against contract text. For each check, answer yes only if the given text itself "
                "states the claimed answer to the question; otherwise no. Answer for every check, using its key.",
                blocks, SUPPORT_SCHEMA, prompt_version=PROMPT_VERSION, **kw)
    got = {c["key"]: c["supported"] == "yes" for c in r["data"]["checks"]}
    return {k: got.get(k, False) for k, _ in items}, [r]


# ---- assembling gold-shaped atoms --------------------------------------------------------------------------------

def _needed(template, v):
    """Atoms that must be known (given the known values) before the rules may run on this family."""
    if template == "coc":
        need = ["whose", "triggers", "effects"]
        t = v.get("triggers") or []
        need += (["threshold_pct", "threshold_op"] if "ownership_threshold" in t else [])
        need += (["retained_holder", "retained_pct"] if "retained_stake" in t else [])
        need += (["transaction_types"] if "transaction_type" in t else [])
        need += (["open_texture"] if "open_textured" in t else [])
        if v.get("qipo_test") == "proceeds":
            need += ["qipo_threshold", "post_ipo_threshold_pct"]
        return need
    if template == "assign":
        return ["restricted"] + (["consent_needed"] if v.get("restricted") == "yes" else [])
    mode = v.get("mode")
    need = ["mode"]
    if mode != "conditional":
        need += ["end_date"] if v.get("end_date") else ["start", "initial_term_n", "initial_term_unit"]
    return need


def assemble(template, atoms, guard_hit=False):
    """-> (family dict in gold shape, or None when the clause is absent). `_unknown` lists blockers for the lawyer."""
    ok = {k: a["value"] for k, a in atoms.items() if a["status"] == "known"}
    pres = atoms["present"]
    if pres["status"] in ("silent",) or ok.get("present") == "no":
        return ({"_unknown": ["silence guard: keywords found but no clause extracted"]} if guard_hit else None)
    if pres["status"] != "known":
        return {"_unknown": [f"present is {pres['status']}"]}
    a, bad = {}, []
    for k in _needed(template, ok):
        if k not in ok:
            bad.append(f"{k} is {atoms[k]['status']}")
    if template == "coc":
        for k in ("whose", "threshold_pct", "threshold_op", "retained_holder", "retained_pct", "transaction_types", "carve_outs",
                  "open_texture", "definition_location", "post_ipo_threshold_pct"):
            if k in ok:
                a[k] = ok[k]
        if ok.get("post_ipo_relative_to_retained") == "yes":
            a["post_ipo_relative_to_retained"] = True
        if "triggers" in ok:
            a["triggers"] = ok["triggers"]
        if "qipo_test" in ok:
            a["qualifying_ipo"] = {"test": ok["qipo_test"], **({"threshold": ok["qipo_threshold"]} if "qipo_threshold" in ok else {})}
        effs = [{"effect": e["effect"], **({"effect_holder": e["effect_holder"]} if e["effect_holder"] else {}),
                 **({"notice": [e["notice_n"], e["notice_unit"]]} if e["notice_n"] is not None and e["notice_unit"] else {})}
                for e in ok.get("effects", [])]
        if len(effs) == 1:
            a.update(effs[0])
        elif effs:
            a["effects"] = effs
    elif template == "assign":
        for k in ("restricted", "consent_needed"):
            if k in ok:
                a[k] = ok[k] == "yes"
        for k in ("coc_not_assignment", "merger_exception", "unless_competitor"):
            if ok.get(k) == "yes":
                a[k] = True
        if ok.get("deems"):
            a["deems"] = ok["deems"]
    else:
        if re.fullmatch(r"\d{4}-\d\d-\d\d", ok.get("start") or ""):
            a["start"] = ok["start"]
        for k in ("end_date", "mode", "option_holder"):
            if k in ok:
                a[k] = ok[k]
        for name, n, u in (("initial_term", "initial_term_n", "initial_term_unit"), ("renewal_term", "renewal_term_n", "renewal_term_unit"),
                           ("notice", "notice_n", "notice_unit")):
            if ok.get(u) == "redacted":
                a[name] = "redacted"
            elif n in ok and u in ok:
                a[name] = [ok[n], ok[u]]
    if bad:
        a["_unknown"] = bad
    return a


# ---- driver ----------------------------------------------------------------------------------------------------------

def compile_one(con_path, cid, template, title, cache_dir, use_cache):
    con = sqlite3.connect(con_path)
    sents = retrieve(con, cid, template)
    full = " ".join(t for _, t in con.execute("SELECT sid, text FROM sentence WHERE contract_id=?", (cid,)))
    con.close()
    guard_hit = bool(re.search(GUARD[template], full, re.I))
    absent = {k: {"status": "silent", "value": None, "sids": [], "k_agree": 0} for k in SPEC[template]}
    if not sents:
        return dict(atoms=absent, support={}, guard=guard_hit, cands=[], calls=[])
    cand = dict(sents)
    kw = dict(cache_dir=cache_dir, use_cache=use_cache)
    sch, sysm, usr = schema(template), system_prompt(template), user_prompt(title, sents)
    calls = [llm.ask(llm.ANSWERER, sysm, usr, sch, seed=i, temperature=TEMP, prompt_version=PROMPT_VERSION, **kw) for i in range(K_RUNS)]
    atoms = vote(template, [clean_run(template, c["data"], cand) for c in calls])
    support, sc = support_check(template, atoms, cand, **kw)
    for k, ok in support.items():
        if not ok:
            atoms[k].update(status="unknown", value=None)
    return dict(atoms=atoms, support=support, guard=guard_hit, cands=[s for s, _ in sents], calls=calls + sc)


GOLD_KEYS = {k.split(":", 1)[1].lower(): k for k in json.loads((ROOT / "cases" / "gold_atoms.json").read_text(encoding="utf-8")) if ":" in k}


def gold_key(cid):
    for name, key in sorted(GOLD_KEYS.items(), key=lambda x: -len(x[0])):
        if cid.startswith(name):
            return key
    return "cuad:" + cid


def run(only="", run_name="main", dry=False, workers=6):
    cache_dir = llm.CACHE if run_name == "main" else ROOT / "out" / f"cache_{run_name}"
    con = sqlite3.connect(DB)
    contracts = [r for r in con.execute("SELECT id, title FROM contract ORDER BY id") if r[0].startswith(only)]
    con.execute("CREATE TABLE IF NOT EXISTS atom(run TEXT, contract_id TEXT, template TEXT, key TEXT, value TEXT, status TEXT, "
                "sids TEXT, models TEXT, k_agree INTEGER, support_ok INTEGER, PRIMARY KEY(run, contract_id, template, key))")
    if dry:
        chars = 0
        for cid, title in contracts:
            for t in SPEC:
                chars += len(system_prompt(t)) + len(user_prompt(title, retrieve(con, cid, t)))
        tin = chars / 4 * (K_RUNS + 0.5)
        tout = len(contracts) * 3 * 900 * (K_RUNS + 0.3)
        print(f"{len(contracts)} contracts x 3 templates: ~{tin / 1e6:.2f}M input tokens, ~{tout / 1e6:.2f}M output tokens, "
              f"est. cost ${tin * 0.3e-6 + tout * 1.3e-6:.2f} (upper-end prices)")
        return
    t0 = time.time()
    jobs = [(cid, t, title) for cid, title in contracts for t in SPEC]
    def safe(j):
        try:
            return compile_one(DB, j[0], j[1], j[2], cache_dir, run_name == "main")
        except llm.BudgetExceeded:
            raise
        except Exception as e:      # a failed call leaves the family unknown, never silent
            print("FAILED", j[0], j[1], type(e).__name__, str(e)[:120])
            return dict(atoms={k: {"status": "unknown", "value": None, "sids": [], "k_agree": 0} for k in SPEC[j[1]]},
                        support={}, guard=False, cands=[], calls=[])
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(safe, jobs))
    out, guards, cost = {}, 0, 0.0
    con.execute("DELETE FROM atom WHERE run=?", (run_name,))
    for (cid, t, _), r in zip(jobs, res):
        models = json.dumps(sorted({f"{c['model']}@{c['provider']}" for c in r["calls"]}))
        fam = assemble(t, r["atoms"], r["guard"])
        if fam and fam.get("_unknown") and fam["_unknown"][0].startswith("silence guard"):
            guards += 1
        if fam is not None:
            out.setdefault(gold_key(cid), {})[t] = fam
        cost += sum(c["cost"] for c in r["calls"] if not c["cached"])
        con.executemany("INSERT INTO atom VALUES(?,?,?,?,?,?,?,?,?,?)", [
            (run_name, cid, t, k, json.dumps(a["value"]), a["status"], json.dumps(a["sids"]), models, a["k_agree"],
             int(r["support"].get(k, False))) for k, a in r["atoms"].items()])
    con.commit()
    (ROOT / "out").mkdir(exist_ok=True)
    name = "compiled_atoms.json" if run_name == "main" else f"compiled_atoms_{run_name}.json"
    (ROOT / "out" / name).write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    st = Counter(row[0] for row in con.execute("SELECT status FROM atom WHERE run=?", (run_name,)))
    print(f"run {run_name}: {len(contracts)} contracts, {len(jobs)} families, {time.time() - t0:.0f}s, new spend ${cost:.3f}, "
          f"total spend ${llm.spent():.3f}, silence guard fired {guards}, atom statuses {dict(st)}")


def _check():
    assert {50.0, 1.0} <= numbers("more than fifty percent (50%) or one (1) year") and 150e6 in numbers("$150 million")
    assert 180.0 in numbers("one hundred eighty (180) days") and 12.0 in numbers("twelve months")
    assert "2013-08-01" in dates("this 1st day of August, 2013") and "1999-11-05" in dates("Nov  5,  1999")
    assert value_matches_text("notice_n", 60, "within sixty (60) days") and not value_matches_text("notice_n", 90, "sixty (60) days")
    cand = {1: "Either party may terminate on a change of control of fifty percent (50%) or more."}
    raw = {k: {"status": "silent", "sids": [], "value": None} for k in COC}
    raw["threshold_pct"] = {"status": "known", "sids": [1], "value": 50}
    raw["effects"] = {"status": "known", "sids": [1], "value": [{"effect": "termination_right", "effect_holder": "them", "notice_n": None, "notice_unit": None}]}
    raw["carve_outs"] = {"status": "known", "sids": [9], "value": ["consented"]}       # sid not a candidate
    r = clean_run("coc", raw, cand)
    assert r["threshold_pct"]["status"] == "known" and r["carve_outs"]["status"] == "unknown"
    raw["threshold_pct"]["value"] = 51
    assert clean_run("coc", raw, cand)["threshold_pct"]["status"] == "unknown"
    assert vote("coc", [r, r, {**r, "threshold_pct": {"status": "known", "value": 40, "sids": [1]}}])["threshold_pct"]["value"] == 50
    assert vote("coc", [r, {**r, "threshold_pct": {"status": "known", "value": 40, "sids": [1]}}, {**r, "threshold_pct": {"status": "silent", "value": None, "sids": []}}])["threshold_pct"]["status"] == "unknown"
    atoms = vote("coc", [r, r, r])
    atoms["present"] = {"status": "known", "value": "yes", "sids": [1], "k_agree": 3}
    assert assemble("coc", atoms)["_unknown"], "missing whose/triggers must block the rules"
    assert assemble("coc", {**atoms, "present": {"status": "known", "value": "no", "sids": [1], "k_agree": 3}}) is None
    print("compile self-check ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--run", default="main")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    _check() if a.check else run(a.only, a.run, a.dry, a.workers)
