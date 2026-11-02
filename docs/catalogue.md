# Variant catalogue and atom design (v1)
Status: built from real clauses (CUAD, EDGAR). NOT reviewed by a lawyer. Each variant cites a source in
data/curated. Atoms are derived from the variants; rules are derived from the atoms (twin/templates).

## A. Change of control: what decides the reaction
### Trigger variants (what counts as a change of control)
| Variant | Example source | Decides |
|---|---|---|
| CT1 ownership threshold, any person (">= 50%", "> 51%", ">= 20%") | RaeSystems, VirtualScopics, DynTek, Zounds | buyer % vs threshold; > vs >= |
| CT2 retained-stake threshold (owners keep >= X%) | JointCorp (75%), Amscan pre-IPO (Sponsors >= 50%) | holders' % after event |
| CT3 two regimes split by a Qualifying IPO (pre: retained stake; post: any person > 35% and >= Sponsors) | Amscan, Calpine, Lantheus | whether a Qualifying IPO happened; proceeds test |
| CT4 control / management (power to direct; board turnover) | iPayment, Zounds (board), Amscan (board) | management_changes flag |
| CT5 transaction type only (merger, consolidation, asset sale) | eDiets, Gopage | event mechanism, not %; share sale excluded |
| CT6 open-textured ("significant", "material", "major shareholding", "unacceptable") | mPhase, EuropeanMicro, eLandia, BlackBox | -> NEEDS_LAWYER except where any reading agrees |
| CT7 undefined "control" | Vertex*, HealthGate* (defined elsewhere / not located) | -> resolve definition, else NEEDS_LAWYER |
| CT8 parent ceases to own 100% of borrower (direct) | Amscan (c) | holding insertion position |
| CT9 cross-reference to another instrument's CoC | Amscan (d) | -> gap (refers_to_missing) |
| CT10 competitor takes a disclosable stake (5% under Rule 13d-1) | VirtualScopics (d) | buyer is competitor and % |
| CT11 change in a named key leadership position | VirtualScopics (e) | management_changes flag |
### Carve-outs
| Carve-out | Source |
|---|---|
| C1 same management team (incl. becoming public) | iPayment |
| C2 Qualifying IPO (proceeds >= $150M, underwritten, SEC-registered) | Amscan |
| C3 prior holders keep >= 50% of successor | eDiets |
| C4 counterparty consented in writing | Zounds, iPayment |
| C5 not a competitor | Corio (assignment), SFG, Energous |
### Effects
termination_right (with notice period) · automatic_termination · consent (prior) · notice (by us, within N days or
business days) · deemed_assignment -> consent · prepayment / event of default (credit) · service_limitation · payment ·
effect defined elsewhere (gap).
### Whose change of control
us / them / either party. Many CUAD clauses are mutual ("either party").

## B. Assignment
| Variant | Source |
|---|---|
| A1 consent needed, silent on CoC | Vertex, HealthGate, eLandia |
| A2 merger / reorganization / operation of law deemed assignment | Gopage, Kiromic, Corio |
| A3 CoC expressly NOT an assignment | VerticalNet |
| A4 assignment allowed without consent on merger/acquisition (except competitor) | Corio |
Key point (all variants): a sale of shares in a party is not an assignment by that party unless the clause deems it.
A holding company inserted by MERGER is caught by 'merger ... involving' language; inserted by SHARE EXCHANGE it is a reading question (NEEDS_LAWYER).

## C. Term and renewal
| Variant | Source |
|---|---|
| R1 auto-renew unless notice X days/months before expiry | VirtualScopics, Gpaq, Corio, eDiets, Zounds |
| R2 evergreen: continues until N months' notice | iPayment |
| R3 option to extend by a party, with advance notice | DynTek, HealthGate, SFG, eLandia |
| R4 mutual agreement only | VerticalNet, BlackBox |
| R5 conditional auto-renew (facts outside text) | EuropeanMicro |
| R6 defective or redacted terms (missing unit, '*') | Vertex, Kiromic |
| R7 start date not stated (on execution) | Agape |

## Atoms (closed answers; every value cites sentence ids)
change_of_control
- present: yes / no
- whose: us / them / either
- trigger_kinds: set of {ownership_threshold, retained_stake, competitor_stake, management_change, transaction_type, open_textured, parent_direct_ownership, cross_reference}
- competitor_threshold_pct: number (default 5)
- threshold_pct: number | silent ; threshold_op: gt / gte
- retained_holder: sponsors / original_owners / principal_owners / none ; retained_pct: number
- transaction_types: set of {merger, consolidation, asset_sale, reorganization, share_sale}
- qualifying_ipo: present / absent ; qipo_test: proceeds / float / exchange / other ; qipo_threshold: number
- post_ipo_threshold_pct: number | none ; post_ipo_relative_to_retained: yes / no
- carve_outs: set of {same_management, qualifying_ipo, prior_holders_keep_50, consented, non_competitor}
- open_texture_term: text | none
- effect: termination_right / automatic_termination / consent / notice / prepayment / service_limitation / elsewhere
- effect_holder: us / them / either ; notice_period: number + unit (days / business_days / months) | none
- unless_consented (per effect): yes / no — prior consent removes this effect only (iPayment: termination right goes, notice duty stays)
- definition_location: in_clause / elsewhere_in_contract / other_document
assignment
- restricted: yes / no ; consent_needed: yes / no
- deems_as_assignment: set of {merger, consolidation, reorganization, operation_of_law, change_of_control} | none
- coc_expressly_not_assignment: yes / no
- merger_exception: yes / no ; exception_unless_competitor: yes / no
term_renewal
- start: date | on_execution | silent ; initial_term: number + unit | end_date | redacted
- mode: auto / evergreen / option / mutual / conditional / none
- option_holder: us / them / either ; renewal_term: number + unit | redacted
- notice_period: number + unit (days / months / business_days / missing_unit) ; notice_anchor: before_expiry / any_time
- condition_outside_text: yes / no
Silent / unknown / conflict / absent / refers_to_missing apply to every atom: silent = the text says nothing; unknown = models disagree or evidence is weak; conflict = two readings; absent = clause not found after a second search; refers_to_missing = depends on a document not in the file.
