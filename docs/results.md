# Results

Proof of concept. Not legal advice. Nothing here is lawyer-reviewed. Every number below is written by `python -m tests.evaluate` from the current code and data into `out/eval/results.json`. Grades: **[measured]** computed by the script from current code and data; **[source]** read from a stored model run; **[unverified]** not checked.

## Setup

- Data: 27 CUAD v1 contracts (CC BY 4.0, The Atticus Project), 3 SEC EDGAR credit-agreement amendments, 8 Arabic texts. 10,810 sentences in the database [measured].
- Models (open weights, via OpenRouter): answers `deepseek/deepseek-v4.1-flash`, support check `qwen/qwen3.8-27b`, blind check `z-ai/glm-5.3`.
- Hand-labelled by the authors: 37 scenario cases with expected outcomes, gold atoms for the clauses they use, and the portfolio file that names our party in each contract. The same author wrote the cases and the rules.
- Portfolio demo: 38 contracts, 9 scenarios, 612 outcomes per state.

## Scenario cases

| Atoms | Correct | Wrong firm | Firm where we abstain | Abstain | Grade |
|---|---|---|---|---|---|
| Hand-labelled (ceiling) | 37/37 | 0 | 0 | 0 | measured |
| Compiled, no review | 28/37 | 0 | 0 | 9 | measured |
| Compiled, after review walk | 37/37 | 0 | none | none | measured (hand labels play the reviewer) |

Wrong firm means a firm answer that differs from a firm expectation. On the compiled atoms, 104 of 141 atoms (73.8%) equal the hand label, 7 are wrong and 30 are unknown [measured]. The cases that are not correct without review: R01, R10, R12, R15, R17, R18, R19, R20, R26.

## Review effort

| Walk | Blocking answers | Confirmations | Corrections | Steps | End | Wrong firm at any step |
|---|---|---|---|---|---|---|
| One atom per step | 15 | 66 | 8 | 89 | 37/37 | 0 |
| One reasoning-path card per step | 15 | 29 cards (67 atoms) | 8 | 44 | 37/37 | 0 |

Hidden error rate of load-bearing compiled atoms on the cases: 8 of 74 atoms a reviewer confirmed or corrected were wrong (10.8%) [measured]. Compile agreement and the support check did not separate the wrong atoms from the right ones. On the portfolio, the simulated review confirmed 110 atoms and corrected 8 (6.8%) [measured].

Portfolio: 92 reasoning-path cards before review (2.42 per contract, 207 single-atom confirmations, 63 blocking questions); 57 cards after the simulated review (1.5 per contract) [measured]. Steps count reviewer actions, not reviewer time.

## Portfolio outcomes

| State | Firm | Provisional (worst case) | Blocked | Of |
|---|---|---|---|---|
| Compiled, no review | 367 | 39 | 206 | 612 |
| After review (simulated from hand labels) | 394 | 55 | 163 | 612 |

NEEDS_LAWYER reactions by main cause [measured]:

| State | All | Check extraction | Missing document | Missing fact | Decision for counsel |
|---|---|---|---|---|---|
| Compiled, no review | 245 | 158 | 34 | 0 | 53 |
| After review (simulated) | 218 | 113 | 40 | 0 | 65 |

Decisions for counsel: 53 outcomes before review, 65 after. The count rises because settling the extraction shows the real legal forks behind it. Hand labels cover 41 clauses in 27 contracts; 11 contracts (67 clauses) stay as compiled.

## Direct read by a model, against the twin

Baseline: the same model (deepseek/deepseek-v4.1-flash) answers each case from the same retrieved sentences, 3 runs. Firm where we abstain means the model gave a firm answer where our labels say NEEDS_LAWYER. [source: out/baseline.json]

|  | Correct | Wrong firm | Firm where we abstain | Abstain |
|---|---|---|---|---|
| Direct read, majority of 3 | 23 | 3 | 9 | 2 |
| Direct read, run 1 | 24 | 6 | 7 | 0 |
| Direct read, run 2 | 23 | 4 | 9 | 1 |
| Direct read, run 3 | 24 | 4 | 9 | 0 |
| Twin, compiled, no review | 28 | 0 | 0 | 9 |

Run-to-run agreement: the direct read keeps the same status in 30/37 cases and the same status, kind, holder and due date in 22/37. The twin gives the same answer every time for a given compiled snapshot, because the rules are code (it is the compile step that varies; see stability).

Cost [source for the unit cost, measured for compile]: 111 baseline calls cost $0.2019, $0.00182 per call. Compile costs $0.0334 per contract once. Break-even against one direct read per scenario: about 18.4 scenarios per contract.

| Contracts | Scenarios | Direct read, 1 run | Direct read, vote of 3 | Twin (compile once) |
|---|---|---|---|---|
| 30 | 1 | $0.05 | $0.16 | $1.00 |
| 30 | 10 | $0.55 | $1.64 | $1.00 |
| 30 | 100 | $5.46 | $16.38 | $1.00 |
| 1,000 | 1 | $1.82 | $5.46 | $33.40 |
| 1,000 | 10 | $18.20 | $54.60 | $33.40 |
| 1,000 | 100 | $182.00 | $546.00 | $33.40 |

Fairness: the cases, the gold atoms and the rules share one author, so the twin on hand-labelled atoms (37/37) is not independent evidence. The blind check below is the independent part. The cost table assumes every contract has the prompt size of this set and no cache hits.

## Blind check

A model of another family (z-ai/glm-5.3, 3 samples) predicted the outcomes from the clause text alone. It agreed on status for 26 of 34 cases before adjudication [source]. Adjudication found our expectation wrong in 2 cases (R13, R19; fixed) and upheld it in 6 (N03, R15, R16, R21, R24, R25). The adjudication is also ours; no lawyer reviewed it.

## Arabic

Consistency, not accuracy. Arabic twins and machine translations of English sources, compiled by the same pipeline: the same outcome as the English source in 8 of 14 scenario runs, 0 wrong firm answers [measured]. Atoms: 59 equal, 3 differ, 9 unknown on one side.

The lost-threshold case: the DynTek machine translation drops the 20% threshold. The English atom is 20; the Arabic side has no value, blocked by ["threshold_pct is refers_to_missing", "transaction_types is unknown"]. The Arabic side abstains instead of inventing a threshold. Per case (Arabic outcome, English outcome, expected): N06: NEEDS_LAWYER, TRIGGERED, TRIGGERED; R04: NEEDS_LAWYER, TRIGGERED, TRIGGERED.

## Compile stability

Recompile once with a fresh cache, same models and prompts, on the 10 contracts with the most scenario cases (28 cases). Cost $0.3344 [measured].

|  | Differ between the two compiles | Of |
|---|---|---|
| Atoms | 15 | 153 (9.8%) |
| Scenario outcomes, compiled only | 2 | 28 cases (7.1%) |
| Scenario outcomes, after the review walk | 0 | 28 cases (0.0%) |

Compiled only, these cases: first compile 23 correct, 0 wrong firm, 5 abstain; second compile 21 correct, 0 wrong firm, 7 abstain. The review walk needed 89 actions on the first compile and 90 on the second (8 and 8 corrections), and reached 37 and 37 of 37 cases correct (all cases, not only these 28), with 0 wrong firm answers at every step.

## What failed, and why

- Decomposition errors: a vague qualifier ("a material change in ownership or management") was split into separate firm triggers. A guard now drops the listed kinds when the cited text shows the qualifier, and sets triggers to unknown when it reaches only some items.
- Compile instability: two compiles of the same contracts differ in some atoms (see stability). Three-run voting and the support check reduce this; they do not remove it, and agreement did not separate right from wrong atoms.
- Effects checker strictness: the support check rejects many `effects` answers (holder, notice, per-effect consent), so the effects atom is the most common reason for an abstention. This is safe and costly.
- Abstention hides wrong atoms: before confirmation was added, a review that only answered blank questions let wrong known atoms produce wrong firm answers. Safety is measured after review, not before.
- The direct read is firm on cases that depend on a document outside the file or on a reading question, and it changes status between runs.

## Limits

- No lawyer has reviewed the rules, the cases, the gold atoms or the adjudication. Outputs say a clause is potentially triggered under a reading; they are not legal conclusions.
- The case set is small: 37 scenarios on 30 English contracts. A rate such as 75.7% correct has wide uncertainty.
- One author wrote the cases, the gold atoms and the rules. The blind check is the only independent test, and it is a model, not a person.
- Arabic is checked for consistency with the English source, not for accuracy. The reviewed state of the demo is simulated from hand labels, not from a reviewer.
- Three clause types (change of control, assignment, term and renewal) and four event types. Direct reactions only, no cascades.

## How to reproduce

```
python -m twin.ingest --all
python -m tests.test_rules
python -m tests.test_ingest
python -m tests.score_cases
python -m tests.evaluate            # no model calls, rewrites results.json and this report
python -m tests.evaluate stability  # the one recompile run (network, about $0.40)
```

Total spend for the whole project: $6.86 [source: out/cache/spend.json]. Sources: CUAD v1 (The Atticus Project, CC BY 4.0), SEC EDGAR public filings, public Gulf government forms.
