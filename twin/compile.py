"""Compile contracts into typed atoms with an open model. The model only answers fixed questions; code checks every
answer (cited sentences, numbers, dates), a second model checks support, and weak evidence becomes UNKNOWN.

    python -m twin.compile --dry                 # token and cost estimate, no calls
    python -m twin.compile --only ipayment       # contracts whose id starts with the text
    python -m twin.compile [--run run2]          # all contracts; a new --run name uses a fresh cache
    python -m twin.compile --check               # offline self-check
    python -m twin.compile --lang ar --budget 1  # the Arabic contracts -> out/compiled_atoms_ar.json
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

PROMPT_VERSION = "v4"
K_RUNS, TEMP, CAP, DEFS, MAXLEN = 3, 0.7, 40, 8, 1200
STATUSES = ["known", "silent", "unknown", "refers_to_missing"]

# ---- atom definitions: key -> (kind, allowed values, question) -----------------------------------------------------
YN = ["yes", "no"]
EFFECTS = ["termination_right", "automatic_termination", "consent", "notice", "prepayment", "service_limitation",
           "elsewhere"]
COC = {
    "present": ("enum", YN, "Does the contract contain a change-of-control (or change in ownership/control) provision?"),
    "whose": ("enum", ["us", "them", "either"], "Whose change of control is covered: our party, the counterparty, or either?"),
    "triggers": ("set", ["ownership_threshold", "retained_stake", "competitor_stake", "management_change", "transaction_type",
                         "open_textured", "parent_direct_ownership", "cross_reference", "undefined"],
                 "What counts as a change of control. Give every kind that the clause or its definition lists. "
                 "A vague word (significant, material, major, unacceptable) in front of a list governs every item in that list: "
                 "'a material change in the ownership or management of the company' is ONE open_textured trigger, not "
                 "ownership_threshold or management_change. Give a clear kind only when it stands in its own limb without the vague word. "
                 "Kinds: "
                 "ownership_threshold = one person or group acquires a percentage of the voting power or shares; "
                 "retained_stake = the existing owners keep less than a percentage; "
                 "competitor_stake = a competitor of the other party acquires a stake at or above a small percentage, often "
                 "described as a stake that must be publicly disclosed under the securities rules (13d-1 in the US means 5%); "
                 "management_change = the board, the management team, or a named key leadership position changes; "
                 "transaction_type = a kind of deal such as merger or asset sale counts whatever the percentage; "
                 "open_textured = vague words such as significant, material, major or unacceptable; "
                 "parent_direct_ownership = a parent stops owning the borrower directly; "
                 "cross_reference = the definition sits in another document; "
                 "undefined = the term is used but no definition can be found."),
    "threshold_pct": ("num", None, "Ownership percentage that triggers it (ownership_threshold)."),
    "competitor_threshold_pct": ("num", None, "Percentage a competitor must acquire (competitor_stake). Use 5 when the text only refers to a "
                                 "stake that must be publicly disclosed under 13d-1."),
    "threshold_op": ("enum", ["gt", "gte"], "'more than X%' = gt; 'X% or more' / 'at least' = gte."),
    "retained_holder": ("enum", ["sponsors", "original_owners", "principal_owners", "none"], "Whose retained stake is tested (retained_stake)."),
    "retained_pct": ("num", None, "Percentage the retained holder must keep."),
    "transaction_types": ("set", ["merger", "consolidation", "asset_sale", "reorganization", "share_sale"], "Deal types that count (transaction_type)."),
    "qipo_test": ("enum", ["proceeds", "float", "exchange", "other"], "How a Qualifying IPO is defined, if the text has one."),
    "qipo_threshold": ("num", None, "Qualifying IPO minimum proceeds in dollars (e.g. 150000000)."),
    "post_ipo_threshold_pct": ("num", None, "After a Qualifying IPO, percentage held by one person that counts as a change of control."),
    "post_ipo_relative_to_retained": ("enum", YN, "After a Qualifying IPO the holder must also hold at least as much as the retained holder."),
    "carve_outs": ("set", ["same_management", "qualifying_ipo", "prior_holders_keep_50", "consented", "non_competitor"],
                   "Exceptions that make the event not a change of control: same_management = the same management team stays; "
                   "qualifying_ipo = a Qualifying IPO; prior_holders_keep_50 = prior holders keep at least 50% of the successor; "
                   "consented = the clause says the change of control does not occur (or does not count) if the other party "
                   "consented in writing first; non_competitor = the acquirer is not a competitor. If prior consent only removes one "
                   "effect, such as the termination right, use unless_consented on that effect instead of this carve-out."),
    "open_texture": ("enum", ["judgement", "magnitude", "other"], "For open_textured: counterparty's judgement/opinion standard, or a magnitude word (significant/material)."),
    "effects": ("effects", None, "What the clause does on a change of control. One entry per effect. notice_n/notice_unit = days or "
                "months (days, months, business_days) within which a notice or termination must be given or takes effect. "
                "effect_holder = who may use or must perform the effect. unless_consented = true when prior written consent of the "
                "other party removes this effect only (the other effects stay)."),
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
ATOMS = {"coc": COC, "assign": ASSIGN, "term": TERM}
INTRO = {"coc": "change of control", "assign": "assignment", "term": "term and renewal"}

# keyword -> weight; a sentence's score is the sum over distinct keywords it contains
KEYWORDS = {
    "coc": {r"change\s+(of|in)\s+(control|ownership)": 5, r"control": 2, r"merg|consolidat": 2, r"acqui": 2, r"ownership": 2,
            r"voting": 1, r"beneficial": 1, r"sale\s+of\s+(all|substantially)": 2, r"reorganiz": 1, r"shares|equity|stock": 1,
            r"assign|transfer": 1, r"تغيير\s+السيطرة|السيطرة|اندماج|استحواذ|ملكية": 3,
            r"تغيير\s+(في\s+)?(السيطرة|الملكية)": 5, r"يندمج|الدمج|يتحد|بيع\s+جميع": 2,
            r"التصويت|الأوراق\s+المالية|الأسهم|حصص": 1, r"إنهاء|ينتهي|المناصب\s+القيادية|مجلس\s+(ال)?إدارة": 1},
    "assign": {r"assign": 5, r"transfer": 2, r"operation\s+of\s+law": 4, r"delegat": 2, r"successor": 1, r"merg|consolidat": 1,
               r"sublicen": 1, r"consent": 1, r"تنازل|إحالة|نقل\s+الحقوق": 3, r"تفويض|تحويل|نقل|بحكم\s+القانون": 2,
               r"الدمج|يندمج|اندماج": 1},
    "term": {r"renew": 5, r"expir": 4, r"initial\s+term": 5, r"\bterm\b": 1, r"extend|extension": 2, r"terminat": 1, r"notice": 1,
             r"effective\s+date": 2, r"commenc": 1, r"\byears?\b|\bmonths?\b": 1, r"تجديد|انتهاء|مدة|سريان": 3,
             r"يتجدد|تمديد|تستمر|المدة\s+الأولية|تاريخ\s+(النفاذ|السريان)": 3, r"إشعار": 1},
}
# strong words: a hit anywhere in the contract means "silent" cannot be trusted
GUARD = {
    "coc": r"change\s+(of|in)\s+(control|ownership)|controlling\s+interest|sale\s+of\s+(all|substantially)|voting\s+(stock|securities|shares)|(merger|consolidation)\s+(of|with)|تغيير\s+(في\s+)?(السيطرة|الملكية)",
    "assign": r"assign|تنازل|تفويض",
    "term": r"renew|expir|initial\s+term|تجديد|انتهاء|يتجدد|تمديد",
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
               "notice_n": {"type": ["number", "null"]}, "notice_unit": {"type": ["string", "null"], "enum": ["days", "months", "business_days", None]},
               "unless_consented": {"type": "boolean"}}
        return {"type": "array", "items": {"type": "object", "properties": eff, "required": list(eff), "additionalProperties": False}}
    props = {k: {"type": "object", "properties": {"status": {"type": "string", "enum": STATUSES},
                                                 "sids": {"type": "array", "items": {"type": "integer"}},
                                                 "value": vt(kind, vals)},
                 "required": ["status", "sids", "value"], "additionalProperties": False}
             for k, (kind, vals, _) in ATOMS[template].items()}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def system_prompt(template):
    lines = "\n".join(f"- {k} ({kind}{': ' + '/'.join(vals) if vals else ''}): {q}" for k, (kind, vals, q) in ATOMS[template].items())
    return (f"You extract facts about the {INTRO[template]} provisions of ONE contract from numbered sentences. "
            "You do not decide legal outcomes. For every atom return status, sids and value.\n"
            "status: known = the cited sentences state it; silent = the sentences you were given say nothing about it; "
            "unknown = the text is unclear, conflicting or you are not sure; refers_to_missing = it depends on a document "
            "or section that is not in the sentences.\n"
            "sids: ids of the sentences that state the value (at least one when status is known; empty otherwise). "
            "Use only ids that appear in brackets. value: copy numbers and dates from the text; when status is not known "
            "use null (or [] for sets). Never guess. If two readings are possible, answer unknown.\n"
            f"Atoms:\n{lines}")


def user_prompt(title, sents, party=None):
    body = "\n".join(f"[{sid}] {t[:MAXLEN]}" for sid, t in sents)
    us = party or "the company named in the contract title"
    return (f"Contract: {title}\n'us' = {us} (the party whose portfolio this is, as the contract names it); "
            f"'them' = the other party.\n\nSentences:\n{body}")


# ---- retrieval -----------------------------------------------------------------------------------------------------

PARTIES = re.compile(r"between|by\s+and\s+among|entered\s+into|hereinafter|\(the\s+[\"“]|collectively|بين|الطرف", re.I)


def parties(con, cid, n=3):
    """The first sentences of the contract that name the parties: they say who 'us' and 'them' are."""
    rows = con.execute("SELECT sid, text FROM sentence WHERE contract_id=? ORDER BY sid LIMIT 15", (cid,)).fetchall()
    return [(sid, t) for sid, t in rows if PARTIES.search(t)][:n]


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
    if template in ("coc", "assign") and chosen:     # whose change of control, and who holds a right, depend on the parties clause
        chosen |= {sid for sid, _ in parties(con, cid)}
    return [(sid, text[sid]) for sid in sorted(chosen)]


# ---- number and date checks ------------------------------------------------------------------------------------------
WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                    "fifteen sixteen seventeen eighteen nineteen".split())}
WORDS.update({w: 20 + 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())})
SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
AR_WORDS = {"واحد": 1, "واحدة": 1, "اثنين": 2, "اثنان": 2, "سنتين": 2, "شهرين": 2, "يومين": 2, "ثلاث": 3, "ثلاثة": 3,
            "أربع": 4, "أربعة": 4, "خمس": 5, "خمسة": 5, "ست": 6, "ستة": 6, "سبع": 7, "سبعة": 7, "ثمان": 8, "ثماني": 8,
            "ثمانية": 8, "تسع": 9, "تسعة": 9, "عشر": 10, "عشرة": 10, "عشرين": 20, "عشرون": 20, "ثلاثين": 30, "ثلاثون": 30,
            "أربعين": 40, "أربعون": 40, "خمسين": 50, "خمسون": 50, "ستين": 60, "ستون": 60, "سبعين": 70, "سبعون": 70,
            "ثمانين": 80, "ثمانون": 80, "تسعين": 90, "تسعون": 90, "مئة": 100, "مائة": 100}
AR_MONTHS = {"يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "إبريل": 4, "مايو": 5, "يونيو": 6, "يونيه": 6, "يوليو": 7,
             "يوليه": 7, "أغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12}
AR_UNIT = {"days": r"يوم|أيام", "months": r"شهر|أشهر|شهور", "years": r"سنة|سنوات|سنتين|عام|أعوام",
           "business_days": r"أيام\s+(تجارية|عمل)"}


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
    # ponytail: Arabic number words one by one (a leading و/ب/ل is stripped); compounds such as "مئة وثمانين" are not
    # summed, so a value written only in words that way stays unknown. The texts here also give digits.
    for w in re.findall(r"[\u0621-\u064A]+", text):
        for t in (w, w[1:] if w[0] in "وبل" else w):
            if t in AR_WORDS:
                out.add(float(AR_WORDS[t]))
    if re.search(r"اثن[يا]\s+عشر", text):
        out.add(12.0)
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
    for m in re.finditer("(\\d{1,2})\\s+(" + "|".join(AR_MONTHS) + ")\\s+(?:سنة\\s+)?(\\d{4})", text):
        out.add(f"{int(m.group(3))}-{AR_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}")
    # a date followed by هـ is Hijri: it is not read as a Gregorian date
    for m in re.finditer(r"(\d{1,2})/(\d{1,2})/(\d{2,4})(?!\d)(?!\s*ه)", low):
        y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        for mo, d in ((m.group(1), m.group(2)), (m.group(2), m.group(1))):
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                out.add(f"{y}-{int(mo):02d}-{int(d):02d}")
    out.update(re.findall(r"\d{4}-\d\d-\d\d", low))
    return out


UNIT_STEM = {"days": "day", "months": "month", "years": "year", "business_days": "business"}
NUM_KEYS = {"threshold_pct", "competitor_threshold_pct", "retained_pct", "qipo_threshold", "post_ipo_threshold_pct", "initial_term_n", "renewal_term_n", "notice_n"}


def value_matches_text(key, value, text):
    """Code-side check of a model answer against the cited text. True when nothing contradicts it."""
    low = text.lower()
    if key in NUM_KEYS:
        return value is not None and float(value) in numbers(text)
    if key in ("start", "end_date"):
        return value == "on_execution" or value in dates(text)
    if key.endswith("_unit"):
        return value in (None, "redacted", "missing_unit") or UNIT_STEM[value] in low or bool(re.search(AR_UNIT[value], text))
    return True


def periods(text):
    """Notice periods the text states, as (n, unit) with unit days / business_days / months: 'sixty (60) days' -> (60.0, 'days')."""
    out = set()
    low = text.lower()
    unit = r"(business\s+days?|days?|months?)"

    def u(w):
        return "business_days" if w.startswith("business") else ("days" if w.startswith("day") else "months")
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\)?\s*" + unit, low):
        out.add((float(m.group(1).replace(",", "")), u(m.group(2))))
    for m in re.finditer(r"\b([a-z]+(?:[- ][a-z]+)?)\s+(?:\(\d+\)\s*)?" + unit, low):
        ns = numbers(m.group(1))
        if len(ns) == 1:
            out.add((ns.pop(), u(m.group(2))))
    # ponytail: Arabic periods are read only when written with digits ("ستين (٦٠) يوماً"); words alone stay unknown
    for m in re.finditer(r"(\d+)\)?\s*(أيام\s+(?:تجارية|عمل)|يوم|أيام|شهر|أشهر|شهور)", text):
        w = m.group(2)
        out.add((float(m.group(1)), "business_days" if re.match(r"أيام\s", w) else "days" if w in ("يوم", "أيام") else "months"))
    return out


# ---- per-run validation and voting ---------------------------------------------------------------------------------

def canon(v):
    return json.dumps(sorted(v, key=json.dumps) if isinstance(v, list) else v, sort_keys=True)


def clean_run(template, raw, cand):
    """Validate one model answer: sids exist and are candidates, known atoms are cited, numbers match the text."""
    out = {}
    for key, (kind, _, _) in ATOMS[template].items():
        a = raw[key]
        st, sids, val = a["status"], [s for s in a["sids"] if s in cand], a["value"]
        if st == "known":
            if val in (None, []) or (kind == "set" and not val):
                st = "silent"
            elif not sids or len(sids) != len(a["sids"]):
                st = "unknown"
            elif not value_matches_text(key, val, " ".join(cand[s] for s in sids)):
                st = "unknown"
            elif key == "effects":      # a notice the cited text does not state is dropped; the effect itself stays
                per = periods(" ".join(cand[s] for s in sids))
                val = [e if (e["notice_n"], e["notice_unit"]) in per else {**e, "notice_n": None, "notice_unit": None} for e in val]
        if st != "known":
            val, sids = None, []
        out[key] = {"status": st, "value": val, "sids": sids}
    return out


def vote_effects(runs, text):
    """Per-field vote on the effects atom. A kind of effect is kept when >= 2 runs list it; its holder and consent flag are
    voted separately (a field without a majority stays None); the notice period is one the cited text states, read by code."""
    known = [r for r in runs if r["status"] == "known"]
    first = [{e["effect"]: e for e in reversed(r["value"])} for r in known]
    kinds = [c for c, n in Counter(c for f in first for c in f).most_common() if n >= 2]
    if len(known) < 2 or not kinds:
        return None
    sids = sorted({s for r in known for s in r["sids"]})
    per = periods(" ".join(text[s] for s in sids if s in text))

    def major(vals):
        if not vals:
            return None
        v, n = Counter(vals).most_common(1)[0]
        return v if n >= 2 else None
    value = []
    for c in kinds:
        es = [f[c] for f in first if c in f]
        prop = Counter((e["notice_n"], e["notice_unit"]) for e in es if e["notice_n"] is not None and (e["notice_n"], e["notice_unit"]) in per)
        n, u = prop.most_common(1)[0][0] if prop else (None, None)
        value.append({"effect": c, "effect_holder": major([e["effect_holder"] for e in es if e["effect_holder"]]),
                      "notice_n": n, "notice_unit": u, "unless_consented": major([e["unless_consented"] for e in es])})
    return {"status": "known", "value": value, "k_agree": len(known), "sids": sids}


def vote(template, runs, text=None):
    """Majority over the k runs, per atom: >= 2 identical (status, value) wins, else unknown. Effects are voted per field."""
    out = {}
    for key in ATOMS[template]:
        if key == "effects" and (v := vote_effects([r[key] for r in runs], text or {})):
            out[key] = v
            continue
        cnt = Counter((r[key]["status"], canon(r[key]["value"])) for r in runs)
        (st, cv), n = cnt.most_common(1)[0]
        if n < 2 or (st == "silent" and n < len(runs)):     # silent needs every run; a split with silent is unknown
            st, cv, n = "unknown", "null", 0
        agree = [r[key] for r in runs if (r[key]["status"], canon(r[key]["value"])) == (st, cv)]
        out[key] = {"status": st, "value": json.loads(cv) if st == "known" else None, "k_agree": n,
                    "sids": sorted({s for a in agree for s in a["sids"]})}
    return out


# ---- decomposition guards --------------------------------------------------------------------------------------------
VAGUE = r"unacceptable|significant|material|major|substantial|جوهري|كبير|غير\s+مقبول"
GOVERNED = {"management_change": r"manag|control|board|الإدارة|الادارة|السيطرة|مجلس",
            "ownership_threshold": r"owner|control|shares|equity|الملكية|السيطرة|الأسهم"}
AUTO = re.compile(r"(shall|will)\s+(immediately\s+|automatically\s+|forthwith\s+)*terminate|agreement\s+(shall\s+)?terminates?\b|"
                  r"ينتهي\s+هذا\s+(العقد|الاتفاق)\s+تلقائي", re.I)
RIGHT = re.compile(r"\bmay\s+(\w+\s+){0,4}terminate|right\s+to\s+terminate|at\s+its\s+option[^.]*terminate|يحق\s+[^.]*إنهاء", re.I)


def guard_triggers(atoms, text):
    """A vague qualifier in front of a list governs the whole list. When the cited text puts one before a management or
    ownership item, only open_textured stays; a qualifier the code cannot tie to the listed kinds makes triggers unknown."""
    t = atoms["triggers"]
    kinds = set(t["value"] or [])
    extra = kinds & set(GOVERNED)
    if t["status"] != "known" or "open_textured" not in kinds or not extra:
        return
    sents = " ".join(text[s] for s in t["sids"] if s in text)
    if not re.search(VAGUE, sents, re.I):
        return
    # the qualifier reaches an item when no clause break (; or a numbered limb) lies between them
    reach = {k for k in extra if re.search(rf"({VAGUE})[^;.]{{0,80}}?({GOVERNED[k]})", sents, re.I)}
    if reach == extra:
        t["value"] = sorted(kinds - extra)
    else:
        t.update(status="unknown", value=None)


def guard_effects(atoms, text):
    """The cited wording decides between a termination right and an automatic end; a contradicting vote becomes unknown.
    Wording that matches both patterns or neither leaves the vote alone."""
    e = atoms["effects"]
    if e["status"] != "known":
        return
    sents = " ".join(text[s] for s in e["sids"] if s in text)
    auto, right = bool(AUTO.search(sents)), bool(RIGHT.search(sents))
    kinds = {x["effect"] for x in e["value"]}
    if (auto and not right and "termination_right" in kinds and "automatic_termination" not in kinds) or        (right and not auto and "automatic_termination" in kinds and "termination_right" not in kinds):
        e.update(status="unknown", value=None)


# ---- support check ---------------------------------------------------------------------------------------------------
def support_schema(keys):
    """The checker may answer only for the keys it was asked about."""
    return {"type": "object", "properties": {"checks": {"type": "array", "items": {
        "type": "object", "properties": {"key": {"type": "string", "enum": list(keys)}, "supported": {"type": "string", "enum": YN}},
        "required": ["key", "supported"], "additionalProperties": False}}}, "required": ["checks"], "additionalProperties": False}


def claim(key, value):
    """What the checker is shown. For effects only the fields that were decided: empty fields are not claims."""
    if key != "effects":
        return value
    return [{f: v for f, v in e.items() if v not in (None, False)} for e in value]


def support_check(template, atoms, text, party=None, parties_text=(), **kw):
    """-> ({key: True / False / None}, calls). None = the checker gave no usable answer for that key (after one retry)."""
    items = [(k, a) for k, a in atoms.items() if a["status"] == "known"]
    if not items:
        return {}, []
    keys = [k for k, _ in items]
    head = (f"'us' = {party or 'the company named in the contract title'}; 'them' = the other party.\n"
            + ("Parties clause:\n" + "\n".join(f"- {t[:MAXLEN]}" for _, t in parties_text) + "\n\n" if parties_text else "\n"))
    blocks = head + "\n\n".join(f"Check {k}\nQuestion: {ATOMS[template][k][2]}\nClaimed answer: {json.dumps(claim(k, a['value']))}\n"
                                f"Text:\n" + "\n".join(f"- {text[s][:MAXLEN]}" for s in a["sids"]) for k, a in items)
    system = ("You verify claims against contract text. For each check, answer yes only if the given text itself "
              "states the claimed answer to the question; otherwise no. Answer for every check, using its key exactly.")
    got, calls = {}, []
    for seed in (0, 1):     # one retry when a key got no answer
        r = llm.ask(llm.CHECKER, system, blocks, support_schema(keys), seed=seed, prompt_version=PROMPT_VERSION, **kw)
        calls.append(r)
        got = {c["key"]: c["supported"] == "yes" for c in r["data"]["checks"]}
        if all(k in got for k in keys):
            break
    return {k: got.get(k) for k in keys}, calls


# ---- assembling gold-shaped atoms --------------------------------------------------------------------------------

def _needed(template, v):
    """Atoms that must be known (given the known values) before the rules may run on this family."""
    if template == "coc":
        need = ["whose", "triggers", "effects"]
        t = v.get("triggers") or []
        need += (["threshold_pct", "threshold_op"] if "ownership_threshold" in t else [])
        need += (["retained_holder", "retained_pct"] if "retained_stake" in t else [])
        need += (["competitor_threshold_pct"] if "competitor_stake" in t else [])
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
        if atoms["carve_outs"]["status"] in ("unknown", "refers_to_missing"):     # an exception that may apply is not absent
            bad.append(f"carve_outs is {atoms['carve_outs']['status']}")
        for k in ("whose", "threshold_pct", "competitor_threshold_pct", "threshold_op", "retained_holder", "retained_pct", "transaction_types", "carve_outs",
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
                 **({"notice": [e["notice_n"], e["notice_unit"]]} if e["notice_n"] is not None and e["notice_unit"] else {}),
                 **({"unless_consented": True} if e.get("unless_consented") else {})}
                for e in ok.get("effects", [])]
        for e in ok.get("effects", []):
            if e.get("unless_consented") is None:       # votes split: the rules must not guess whether consent matters
                bad.append(f"unless_consented is unknown for {e['effect']}")
        if len(effs) == 1 and not effs[0].get("unless_consented"):
            a.update(effs[0])
        elif effs:
            a["effects"] = effs
        gaps = [f"effect_holder unknown for {e['effect']}" for e in ok.get("effects", []) if not e["effect_holder"]]
        if gaps:
            a["_partial"] = gaps
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
    # atoms the rules do not need but whose vote was unknown stay visible to the review queue
    skip = {b.split(" is ")[0] for b in bad}
    opt = [f"{k} is {x['status']}" for k, x in atoms.items() if x["status"] in ("unknown", "refers_to_missing") and k not in skip and k != "present"]
    if opt:
        a["_optional_unknown"] = opt
    return a


# ---- driver ----------------------------------------------------------------------------------------------------------

def compile_one(con_path, cid, template, title, cache_dir, use_cache, party=None, budget=llm.BUDGET_USD):
    con = sqlite3.connect(con_path)
    sents = retrieve(con, cid, template)
    pt = parties(con, cid)
    full = " ".join(t for _, t in con.execute("SELECT sid, text FROM sentence WHERE contract_id=?", (cid,)))
    con.close()
    guard_hit = bool(re.search(GUARD[template], full, re.I))
    absent = {k: {"status": "silent", "value": None, "sids": [], "k_agree": 0} for k in ATOMS[template]}
    if not sents:
        return dict(atoms=absent, support={}, guard=guard_hit, cands=[], calls=[])
    cand = dict(sents)
    kw = dict(cache_dir=cache_dir, use_cache=use_cache, budget=budget)
    sch, sysm, usr = schema(template), system_prompt(template), user_prompt(title, sents, party)
    calls = [llm.ask(llm.ANSWERER, sysm, usr, sch, seed=i, temperature=TEMP, prompt_version=PROMPT_VERSION, **kw) for i in range(K_RUNS)]
    atoms = vote(template, [clean_run(template, c["data"], cand) for c in calls], cand)
    if template == "coc":
        guard_triggers(atoms, cand)
        guard_effects(atoms, cand)
    support, sc = support_check(template, atoms, cand, party, pt, **kw)
    for k, ok in support.items():
        if ok is not True:      # rejected (False) or no usable reply (None): unknown, never silent
            atoms[k].update(status="unknown", value=None)
    return dict(atoms=atoms, support=support, guard=guard_hit, cands=[s for s, _ in sents], calls=calls + sc)


GOLD_KEYS = {k.split(":", 1)[1].lower(): k for k in json.loads((ROOT / "cases" / "gold_atoms.json").read_text(encoding="utf-8")) if ":" in k}


def gold_key(cid):
    for name, key in sorted(GOLD_KEYS.items(), key=lambda x: -len(x[0])):
        if cid.startswith(name):
            return key
    return "cuad:" + cid


PORTFOLIO = {k: v for f in ("portfolio.json", "portfolio_ar.json")
             for k, v in json.loads((ROOT / "cases" / f).read_text(encoding="utf-8"))["contracts"].items()}


def run(only="", run_name="main", dry=False, workers=6, extra_budget=3.0, lang="en"):
    cache_dir = llm.CACHE if run_name == "main" else ROOT / "out" / f"cache_{run_name}"
    budget = llm.spent() + extra_budget          # a cap on what this run may add
    con = sqlite3.connect(DB)
    contracts = [r for r in con.execute("SELECT id, title FROM contract WHERE language=? ORDER BY id", (lang,))
                 if r[0].startswith(only) and r[0] in PORTFOLIO]
    tag = run_name if lang == "en" else f"{run_name}_{lang}"     # atom-table run name; English rows stay untouched
    con.execute("CREATE TABLE IF NOT EXISTS atom(run TEXT, contract_id TEXT, template TEXT, key TEXT, value TEXT, status TEXT, "
                "sids TEXT, models TEXT, k_agree INTEGER, support_ok INTEGER, PRIMARY KEY(run, contract_id, template, key))")
    if dry:
        chars = 0
        for cid, title in contracts:
            for t in ATOMS:
                chars += len(system_prompt(t)) + len(user_prompt(title, retrieve(con, cid, t), PORTFOLIO[cid]["our_party"]))
        tin = chars / 4 * (K_RUNS + 0.5)
        tout = len(contracts) * 3 * 900 * (K_RUNS + 0.3)
        print(f"{len(contracts)} contracts x 3 templates: ~{tin / 1e6:.2f}M input tokens, ~{tout / 1e6:.2f}M output tokens, "
              f"est. cost ${tin * 0.3e-6 + tout * 1.3e-6:.2f} (upper-end prices)")
        return
    t0 = time.time()
    jobs = [(cid, t, title) for cid, title in contracts for t in ATOMS]
    def safe(j):
        try:
            return compile_one(DB, j[0], j[1], j[2], cache_dir, run_name == "main", PORTFOLIO[j[0]]["our_party"], budget)
        except llm.BudgetExceeded:
            raise
        except Exception as e:      # a failed call leaves the family unknown, never silent
            print("FAILED", j[0], j[1], type(e).__name__, str(e)[:120])
            return dict(atoms={k: {"status": "unknown", "value": None, "sids": [], "k_agree": 0} for k in ATOMS[j[1]]},
                        support={}, guard=False, cands=[], calls=[])
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(safe, jobs))
    out, guards, cost = {}, 0, 0.0
    con.execute("DELETE FROM atom WHERE run=?", (tag,))
    for (cid, t, _), r in zip(jobs, res):
        models = json.dumps(sorted({f"{c['model']}@{c['provider']}" for c in r["calls"]}))
        fam = assemble(t, r["atoms"], r["guard"])
        if fam and fam.get("_unknown") and fam["_unknown"][0].startswith("silence guard"):
            guards += 1
        if fam is not None:
            out.setdefault(gold_key(cid) if lang == "en" else PORTFOLIO[cid]["src"], {})[t] = fam
        cost += sum(c["cost"] for c in r["calls"] if not c["cached"])
        con.executemany("INSERT INTO atom VALUES(?,?,?,?,?,?,?,?,?,?)", [
            (tag, cid, t, k, json.dumps(a["value"]), a["status"], json.dumps(a["sids"]), models, a["k_agree"],
             int(r["support"].get(k) is True)) for k, a in r["atoms"].items()])
    con.commit()
    (ROOT / "out").mkdir(exist_ok=True)
    name = "compiled_atoms" + ("" if run_name == "main" else f"_{run_name}") + ("" if lang == "en" else f"_{lang}") + ".json"
    (ROOT / "out" / name).write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    st = Counter(row[0] for row in con.execute("SELECT status FROM atom WHERE run=?", (tag,)))
    print(f"run {tag}: {len(contracts)} contracts, {len(jobs)} families, {time.time() - t0:.0f}s, new spend ${cost:.3f}, "
          f"total spend ${llm.spent():.3f}, silence guard fired {guards}, atom statuses {dict(st)}")


def _check():
    assert {50.0, 1.0} <= numbers("more than fifty percent (50%) or one (1) year") and 150e6 in numbers("$150 million")
    assert 180.0 in numbers("one hundred eighty (180) days") and 12.0 in numbers("twelve months")
    assert "2013-08-01" in dates("this 1st day of August, 2013") and "1999-11-05" in dates("Nov  5,  1999")
    assert value_matches_text("notice_n", 60, "within sixty (60) days") and not value_matches_text("notice_n", 90, "sixty (60) days")
    cand = {1: "Either party may terminate on a change of control of fifty percent (50%) or more."}
    raw = {k: {"status": "silent", "sids": [], "value": None} for k in COC}
    raw["threshold_pct"] = {"status": "known", "sids": [1], "value": 50}
    raw["effects"] = {"status": "known", "sids": [1], "value": [{"effect": "termination_right", "effect_holder": "them", "notice_n": None, "notice_unit": None, "unless_consented": False}]}
    raw["carve_outs"] = {"status": "known", "sids": [9], "value": ["consented"]}       # sid not a candidate
    r = clean_run("coc", raw, cand)
    assert r["threshold_pct"]["status"] == "known" and r["carve_outs"]["status"] == "unknown"
    raw["threshold_pct"]["value"] = 51
    assert clean_run("coc", raw, cand)["threshold_pct"]["status"] == "unknown"
    assert vote("coc", [r, r, {**r, "threshold_pct": {"status": "known", "value": 40, "sids": [1]}}])["threshold_pct"]["value"] == 50
    assert vote("coc", [r, {**r, "threshold_pct": {"status": "known", "value": 40, "sids": [1]}}, {**r, "threshold_pct": {"status": "silent", "value": None, "sids": []}}])["threshold_pct"]["status"] == "unknown"
    sil = {"status": "silent", "value": None, "sids": []}
    kn = {"status": "known", "value": ["consented"], "sids": [1]}
    assert vote("coc", [{**r, "carve_outs": sil}, {**r, "carve_outs": sil}, {**r, "carve_outs": kn}])["carve_outs"]["status"] == "unknown"
    assert vote("coc", [{**r, "carve_outs": sil}] * 3)["carve_outs"]["status"] == "silent"
    assert support_schema(["a", "b"])["properties"]["checks"]["items"]["properties"]["key"]["enum"] == ["a", "b"]
    atoms = vote("coc", [r, r, r])
    atoms["present"] = {"status": "known", "value": "yes", "sids": [1], "k_agree": 3}
    assert assemble("coc", atoms)["_unknown"], "missing whose/triggers must block the rules"
    fam = assemble("coc", {**atoms, "open_texture": {"status": "unknown", "value": None, "sids": [], "k_agree": 0}})
    assert "open_texture is unknown" in fam["_optional_unknown"] and "open_texture is unknown" not in fam["_unknown"]
    fam = assemble("coc", {**atoms, "carve_outs": {"status": "unknown", "value": None, "sids": [], "k_agree": 0}})
    assert "carve_outs is unknown" in fam["_unknown"] and "carve_outs is unknown" not in fam.get("_optional_unknown", [])
    assert assemble("coc", {**atoms, "present": {"status": "known", "value": "no", "sids": [1], "k_agree": 3}}) is None
    assert {(60.0, "days"), (3.0, "business_days"), (6.0, "months")} <= periods("within sixty (60) days, three (3) Business Days or 6 months")
    eff = lambda h, n, u, c=False: {"effect": "termination_right", "effect_holder": h, "notice_n": n, "notice_unit": u, "unless_consented": c}
    txt = {1: "may terminate on 180 days notice"}
    mk = lambda e: {"status": "known", "value": [e], "sids": [1]}
    v = vote_effects([mk(eff("them", 180, "days")), mk(eff("them", 90, "days")), mk(eff("us", None, None))], txt)["value"][0]
    assert v["effect_holder"] == "them" and (v["notice_n"], v["notice_unit"]) == (180, "days") and v["unless_consented"] is False
    v = vote_effects([mk(eff("them", None, None)), mk(eff("us", None, None)), {"status": "silent", "value": None, "sids": []}], txt)["value"][0]
    assert v["effect_holder"] is None, "split holder stays unknown while the effect is known"
    assert vote_effects([mk(eff("them", None, None))] + [{"status": "silent", "value": None, "sids": []}] * 2, txt) is None
    # Arabic: Arabic-Indic digits, number words, units, Gregorian month names; a Hijri date is never read as Gregorian
    assert {50.0, 10.0} <= numbers("لنسبة ٥٠٪ (خمسين في المائة) ... عشر (١٠) سنوات") and 12.0 in numbers("لمدة اثني عشر شهراً")
    assert 60.0 in numbers("قبل نهاية الفترة بستين (60) يوماً") and 1.0 in numbers("مدة كل منها سنة واحدة")
    assert dates("الموافق 14/11/1431هـ، الموافق 22/10/2010م") == {"2010-10-22"}
    assert dates("اعتباراً من ٣ أكتوبر ٢٠١٨م") == {"2018-10-03"} and "2007-01-29" in dates("المؤرخة في 29 يناير 2007")
    assert dates("اليوم الأول من يونيو سنة 1999") == set(), "an ordinal day is not parsed: the start stays unknown"
    assert periods("قبل ثلاثين (٣٠) يوماً") == {(30.0, "days")} and (3.0, "business_days") in periods("خلال ثلاثة (3) أيام تجارية")
    assert (6.0, "months") in periods("قبل ستة (6) أشهر على الأقل") and (180.0, "days") in periods("مئة وثمانين (180) يوماً")
    assert value_matches_text("initial_term_unit", "years", "لمدة سنتين (2)") and not value_matches_text("notice_unit", "months", "ستين (٦٠) يوماً")
    q = {1: "Termination applies on an unacceptable change in the control or management of a party."}
    mk = lambda tr: {"triggers": {"status": "known", "value": tr, "sids": [1]}}
    a = mk(["open_textured", "management_change"]); guard_triggers(a, q)
    assert a["triggers"]["value"] == ["open_textured"], "the qualifier covers the whole list"
    q = {1: "Either party may terminate if a person acquires 50% of the shares, or if there is a material change in the management."}
    a = mk(["open_textured", "management_change", "ownership_threshold"]); guard_triggers(a, q)
    assert a["triggers"]["status"] == "unknown", "the qualifier reaches only one of two items: not a guess"
    q = {1: "A change of control occurs if the board changes, or a material adverse event happens."}
    a = mk(["open_textured", "management_change"]); guard_triggers(a, q)
    assert a["triggers"]["status"] == "unknown", "qualifier present but not in front of management"
    a = mk(["management_change"]); guard_triggers(a, q)
    assert a["triggers"]["value"] == ["management_change"]
    ef = lambda k, t: {"effects": {"status": "known", "sids": [1], "value": [{"effect": k}]}} | {"_t": t}
    for kind, wording, bad in (("termination_right", "This Agreement shall automatically terminate.", True),
                               ("automatic_termination", "The other party may terminate this Agreement.", True),
                               ("termination_right", "The other party shall have the right to terminate.", False),
                               ("automatic_termination", "This Agreement terminates on that date.", False),
                               ("termination_right", "The parties shall review the Agreement.", False),
                               ("automatic_termination", "ينتهي هذا العقد تلقائياً.", False), ("termination_right", "ينتهي هذا العقد تلقائياً.", True),
                               ("automatic_termination", "يحق للطرف الآخر إنهاء هذا العقد.", True)):
        a = ef(kind, wording); guard_effects(a, {1: wording})
        assert (a["effects"]["status"] == "unknown") == bad, (kind, wording)
    print("compile self-check ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--run", default="main")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--lang", default="en", help="contract language in the database: en or ar")
    ap.add_argument("--budget", type=float, default=3.0, help="USD this run may add to the total spend")
    a = ap.parse_args()
    _check() if a.check else run(a.only, a.run, a.dry, a.workers, a.budget, a.lang)
