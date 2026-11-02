# Contract twin

A proof of concept for running "what if" events over a whole contract portfolio, deterministically, on open-weight
models that can be self-hosted.

Ask: *"If 60% of our shares are sold, or we list, or we insert a holding company, or the clock moves 90 days — which
contracts need consent, which need notice, who can terminate, which deadlines start?"* Get each answer with the
clause text that caused it, the same answer every run, and an explicit **NEEDS_LAWYER** wherever the text leaves a
real question open.

Not legal advice. Outputs say "clause X is potentially triggered under reading Y". Nothing here is lawyer-reviewed.

## How it works
```
contract text ─► ingest ─► compile (open LLM, once) ─► atoms ─► rules (Python, reviewed) ─► reactions + evidence
                 sentences    typed answers + cited       │        three-valued logic            ▲
                 + offsets    sentence ids, checked       │                                      │
                                                         events (share sale, IPO, holding insertion, date)
```
1. **Ingest** splits each contract into sentences with exact character offsets, maps defined terms to the sentence
   that defines them, and stores everything in SQLite.
2. **Compile** asks an open-weight model a fixed set of closed questions per clause type ("atoms": threshold %, who
   may terminate, notice period, carve-outs...). Every answer must cite sentence ids; a second model checks that the
   cited text supports the answer; numbers and dates are parsed by code; disagreement or weak evidence -> UNKNOWN.
   This runs once per contract.
3. **Rules** are plain Python per clause type, written from a catalogue of real drafting variants
   (`docs/catalogue.md`). They use strong Kleene logic (true / false / unknown): any outcome that depends on an
   unknown becomes NEEDS_LAWYER instead of a guess.
4. **Events** update one company state (who owns what, listed or not, IPO proceeds, management changed, structure,
   date). Each contract's rules read that state, so contract-specific tests ("is this IPO a Qualifying IPO?") are
   decided per contract. Running a new scenario needs no model call.

Why this split: open models are weak at writing legal logic themselves but strong at answering narrow questions
inside reviewed logic; the model is never asked what a contract "does".

## Data
| Set | What | Source / licence |
|---|---|---|
| `data/curated/cuad/` | 27 real commercial contracts (22 with change-of-control, anti-assignment and renewal labels; 5 mentioning an IPO) | CUAD v1, The Atticus Project, CC BY 4.0 |
| `data/curated/edgar/` | 3 credit-agreement amendments with "Qualifying IPO" change-of-control definitions | SEC EDGAR public filings |
| `data/curated/arabic/` | 8 Arabic texts: Dubai unified tenancy contract, 2 Saudi model contracts, 3 Arabic contracts written to mirror CUAD clauses, 2 machine translations | Government forms (no licence stated; public), own writing, Qwen translations |
| `cases/scenario_cases.json` | 37 scenarios on real clauses with expected outcomes written before the rules | own |
| `cases/gold_atoms*.json` | hand-labelled atoms per clause | own, not lawyer-reviewed |

Two of the Saudi PDFs have a broken font map (about a third of the words come out garbled); they are kept as-is as a
realistic messy input.

## Results so far
| Check | Result |
|---|---|
| Rules vs scenario cases, on hand-labelled atoms | 37/37 [measured] — same author wrote cases and rules; see the blind check |
| Blind check: an open model of another family (GLM 5.3, 3 samples) predicts the outcomes from the clause text alone | agreed 26/34 before adjudication [measured]; adjudication found 2 errors on our side (fixed) and upheld 6 |
| Exhaustive truth table, change of control | 15,552 combinations; 180-row review view; 5 metamorphic properties and threshold boundary checks hold [measured] |
| Ingest | 30 English contracts, 9,753 sentences, offsets exact for all; 205/205 CUAD label pieces located [measured] |
| Compilation with an open model | in progress |

## Run
Windows or any Python 3.11 with `pip install -r requirements.txt`, from the repo root:
```
python -m twin.ingest --all     # build data/twin.sqlite
python -m tests.test_ingest     # offsets, defined terms, label spans
python -m tests.test_rules      # truth tables + metamorphic properties (writes out/)
python -m tests.score_cases     # rules vs the scenario cases
```
Compilation needs an OpenRouter key in `.env` (never committed).

## Layout
`twin/` code · `docs/catalogue.md` drafting variants and atom design · `cases/` scenarios, gold atoms, blind-check
results · `data/curated/` inputs · `tests/` checks · `tasks/` work specs and reports for contributors · `out/` generated.

## Limits
Proof of concept. Three clause types (change of control, assignment, term/renewal) and four event types. Direct
reactions only (no cascades). No lawyer has reviewed the rules, cases or labels. Arabic is checked for consistency
with the English source, not for accuracy.
