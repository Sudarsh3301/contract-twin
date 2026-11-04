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
| C3 | The simulation is accurate as far as the facts and rules are, and never more. An uncertain fact stops the answer and becomes a lawyer question; it is not passed on as a guess. | Built | 37 cases: 28 answered with no human, 9 held back, 0 wrong firm answers; 37/37 after review. |
| C4 | Scale comes from reuse: each contract is read once, each lawyer answer is reused for every later event, so a new event costs nothing. | Built | About $0.033 per contract once; a new event needs no model call. |
| C5 | Asking a model directly is less reliable than this split. | Built | Same model, same clause text: 3 to 6 wrong firm answers per run, and answers change between runs. |
| C6 | The facts do not depend on language, so one rulebook covers a bilingual portfolio, and two language versions can be checked against each other. | Partial | 8 Arabic texts; same outcome as English in 8 of 14 runs, 0 wrong firm; differences are abstentions. Consistency only, not accuracy. |
| C7 | Within the clause types covered, the check is exhaustive: every combination of fact values has a rule result. | Built | Full truth tables (15,552 rows) and metamorphic tests in `tests/test_rules.py`. |
| C8 | A new or draft contract can be compiled and run through the event library before signing, to see what it would trigger. | Not built | Same pipeline; no draft stress-test yet. |
| C9 | Hostile event sequences and alternative readings can be searched to find the worst case for a portfolio. | Not built | Design only. |

## Scope

In scope: clause types whose logic is closed (change of control, assignment, term and renewal; next: notice, payment
triggers, termination). Deal events from a fixed library. Direct reactions with deadlines. English and Arabic.

Out of scope by design: open-textured wording ("material adverse change", "reasonable efforts"); these go to a
lawyer. Legal advice or conclusions. Drafting. Knock-on effects between contracts. Which language version governs.

## The weak link

The reading step, not the logic. 104 of 141 compiled facts matched the hand labels, 7 were wrong, 30 unknown. A second
compile changed 9.8% of facts (0 outcomes after review). Work that improves reading, or catches wrong facts before
they reach a firm answer, strengthens the thesis most.

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

## What not to claim

Lawyer-reviewed. Hours saved. Tested offline. Accurate in Arabic. Every clause or any event. That results from the
works this builds on carry over. C8 and C9 as working features.

## Parked

Ideas that failed the test. One line each, with the claim they would serve if they come back.
