# Thesis

Read this before adding a feature, an experiment or a research thread. If new work does not test or strengthen a
claim below, it is a tangent. Park it under "Parked" with one line, and do not build it.

## The thesis in one sentence

Read once, decide many times. An open model pulls the facts that decide deal outcomes out of each contract once, as
small cited answers. The legal logic is written once per clause type, as rules. Then any event runs across the whole
portfolio instantly and the same way every time. What the text cannot settle becomes one question for a lawyer,
answered once and reused for every later event.

## The claims

Each claim has a status. Built = in the code and measured. Partial = some evidence. Not built = an idea only.

| # | Claim | Status | Evidence now |
|---|---|---|---|
| C1 | Reading and reasoning are split. The model only answers fixed questions per clause type, each cited to a sentence. The rules are code, written once per clause type, not per contract. | Built | Rules in `twin/rules.py`; every known answer cites a sentence and passes a support check. |
| C2 | Given the converted facts, any event runs over a portfolio of any size deterministically. The same facts and event always give the same answer. | Built | Rules are pure functions; 9 events over 33 agreements in 0.37 s. |
| C3 | The simulation is accurate as far as the facts and rules are, and never more. An uncertain fact stops the answer and becomes a lawyer question; it is not passed on as a guess. | Built | 37 cases: 28 answered with no human, 9 held back, 0 wrong firm answers; 37/37 after review. 0 of 37 still allows a true wrong-answer rate of up to about 8% (95% bound). |
| C4 | Scale comes from reuse: each contract is read once, each lawyer answer is reused for every later event, so a new event costs nothing. | Built | About $0.033 per contract once; a new event needs no model call. |
| C5 | Asking a model directly is less reliable than this split. | Built | Same model, same clause text: 3 to 6 wrong firm answers per run, and answers change between runs. The gain is safety (wrong answers become held-back questions), not raw accuracy: in the literature the formal route also turns wrong answers into abstentions. |
| C6 | The facts do not depend on language, so one rulebook covers a bilingual portfolio, and two language versions can be checked against each other. | Partial | 8 Arabic texts; same outcome as English in 8 of 14 runs, 0 wrong firm; differences are abstentions. Consistency only, not accuracy. |
| C7 | Within the clause types covered, the check is exhaustive: every combination of fact values has a rule result. | Built | Full truth tables (15,552 rows) and metamorphic tests in `tests/test_rules.py`. |
| C8 | A new or draft contract can be compiled and run through the event library before signing, to see what it would trigger. | Not built | Same pipeline; no draft stress-test yet. |
| C9 | Hostile event sequences and alternative readings can be searched to find the worst case for a portfolio. | Not built | Design only. |

## Scope

In scope: clause types whose logic is closed (change of control, assignment, term and renewal; next: notice, payment
triggers, termination). Deal events from a fixed library. Direct reactions with deadlines. English and Arabic.

Out of scope by design: open-textured wording ("material adverse change", "reasonable efforts"); these go to a
lawyer. Legal advice or conclusions. Drafting. Knock-on effects between contracts. Which language version governs.

## Reading once per contract family

"Read once" means once per contract family: the base agreement, its amendments, side letters, schedules and the
documents its defined terms point to. A file read alone can give a confident, stale answer. A document that is
referenced but missing is UNKNOWN, never "absent".

## The weak link

The reading step, not the logic. 104 of 141 compiled facts matched the hand labels, 7 were wrong, 30 unknown. A second
compile changed 9.8% of facts (0 outcomes after review). Work that improves reading, or catches wrong facts before
they reach a firm answer, strengthens the thesis most.

Why agreement did not catch the wrong facts: models make correlated errors. When two models are both wrong they give
the same wrong answer about 60% of the time, and more capable models are more correlated, also across providers
(Kim et al., ICML 2025). A check by a second model, or a vote of three runs, is therefore not an independent check.

## Test for new work

Ask, in this order. Stop at the first "no".

1. Which claim (C1 to C9) does it test or strengthen? Name it. None -> tangent.
2. Does it keep the split? The model must never decide an outcome or write a rule.
3. Does it keep C3? An uncertain fact must still stop the answer, not become a guess.
4. Can a non-technical legal reader see the result on the page or in one number? If not, it is internal work: do it
   only if it fixes the weak link.
5. Is it the smallest version that moves a claim from Partial or Not built toward Built?

Good examples: a new clause type with closed logic (C1, C2); a check that catches wrong facts (weak link, C3); more
Arabic texts with native labels (C6 from consistency to accuracy); a draft stress-test (C8).
Tangents: a chat interface; models writing rules; free-text summaries of contracts; more UI polish that adds no claim;
paper-style ablations that no reader sees; clause types that are mostly judgement.

## Enhancements that serve a claim (ranked)

From a sourced research pass (Nov 2026). Each line names the claim it serves.

1. Independent checks on each fact (weak link, C3): code validators (the number must appear in the cited span; the
   party must resolve to a defined party), a checker from another model family, and a second reader that reads the
   whole clause instead of one question; disagreement is a signal. Evidence: correlated errors (Kim et al., ICML 2025);
   single confidence signals collapse in structured extraction (arXiv 2606.24420, preprint).
2. Set the auto-accept line with a calibrated score and a bigger labelled set (C3). 141 labelled facts cannot set it.
   Evidence: calibrated, decomposed confidence auto-approved 49-72% of fields at under 10% error, against 0.1-7% with
   the model's own confidence (arXiv 2609.20110, preprint).
3. Contract families before compile (C1, C4): see "Reading once per contract family".
4. Model-drafted rules for new clause types, approved by execution, not by reading code (C1, C7): run the draft on every
   combination of fact values and review the truth table; diff two independent drafts. Evidence: model-drafted
   decision models agreed with the correct ones on only 51-53% of cases on average, and similar-looking models often
   behaved differently (Graus, ICAIL 2026). Typical drafting errors: <= against <, dropped "except" clauses,
   invented dates (Catala generation, NLLP 2025).
5. Document reading for real files (C2, C6): Docling (MIT) for born-digital files, a vision OCR model for scans, page
   positions kept. Arabic must be tested: open Docling and MinerU issues show reversed or broken Arabic text.
6. Fully local serving (claims about own servers): vLLM structured outputs, with the JSON checked after decoding.
7. Next clause types, in order of how often deal events touch them: exclusivity, non-compete, MFN, termination for
   convenience, non-solicit (CUAD has 41 clause types; the event-sensitive set is about 8 to 12).
8. Remove hidden text and quarantine instructions found in documents before any model reads them (OWASP LLM01).

## Known cons

- Correlated errors weaken the second-model check (see the weak link).
- Rules drafted by a model often behave differently from the intended rule; a human stays the author of record.
- Coded rules fix one reading of open wording; discretionary terms ("reasonable", "material") go to a lawyer, and
  every answer shows which rule produced it.
- Arabic PDFs: right-to-left order, mirrored brackets and ligatures can come out wrong; OCR Arabic pages and compare
  with the text layer.
- Gulf data residency: the UAE PDPL implementing rules were still unpublished in early 2026; Saudi transfers need
  approved clauses or a risk assessment. A hosted router sends text to third parties, so real Gulf data needs fully
  local inference. The demo uses public text only.
- Hidden text in documents can carry instructions to the model.
- Parser licences: Marker weights and MinerU carry commercial limits; check before use.

## What not to claim

Lawyer-reviewed. Hours saved. Tested offline. Running on our own servers today
(the demo uses a hosted router for open-weight models; self-hostable is the accurate word). That the formal route is
more accurate than a model (it is safer: it holds back instead of answering wrong). Accurate in Arabic. Every clause or any event. That results from the
works this builds on carry over. C8 and C9 as working features.

## Parked

Ideas that failed the test. One line each, with the claim they would serve if they come back.

- Upload box on the public site: breaks the own-servers story and needs a server holding the key. A local "drop a
  folder in" command serves C2 and C4 instead.

## Sources for the research pass

Kim et al., Correlated Errors in LLMs, ICML 2025: https://arxiv.org/abs/2506.07962 ·
Graus, ICAIL 2026: https://arxiv.org/html/2604.17153 · SARA formalisation: https://arxiv.org/html/2508.21051v3 ·
Catala generation, NLLP 2025: https://aclanthology.org/2025.nllp-1.4/ · ExtractConf: https://arxiv.org/abs/2606.24420 ·
Calibrated extraction confidence: https://arxiv.org/abs/2609.20110 · KITAB-Bench (Arabic OCR): https://arxiv.org/abs/2502.14949 ·
Docling Arabic issues: https://github.com/docling-project/docling/issues/3462 · vLLM structured outputs:
https://docs.vllm.ai/en/latest/features/structured_outputs/ · OWASP LLM01: https://genai.owasp.org/llmrisk/llm01-prompt-injection/ ·
UAE transfers: https://www.kayrouzandassociates.com/insights/cross-border-data-transfers-under-uae-law-in-2026 ·
Saudi transfers: https://www.mayerbrown.com/en/insights/publications/2024/10/updates-to-saudi-arabias-personal-data-protection-regulations-sccs-guidelines-and-more ·
Amendment chains (vendor): https://robinai.com/news-and-resources/blog/the-hidden-network-behind-your-contracts ·
CUAD: https://www.atticusprojectai.org/cuad
