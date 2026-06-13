# Hostile_Synthetic - GROUND TRUTH (answer key)

PLATFORM MUST NEVER READ THIS FILE. It exists only so humans can score what the pipeline
produced. It lives under `generator/` (no kpi/model/dictionary filename hints) precisely so the
classifier never treats it as a workspace input.

Generator: `generate.py`, seed `20260612`, stdlib-only, fully deterministic.
57 tables / 30,564 rows. Domain: UK road logistics (Brindle & Vance Logistics).

## Designed hostility

1. **Column-name collisions.** `Id`, `Name`, `Date`, `START`, `END`, `Amount`, `Status` recur
   with different meanings: `shipments.Amount` = quoted charge, `invoices.Amount` = billed
   revenue, `movements.Amount` = internal leg cost, `carriers.Amount` = insurance coverage,
   `contracts.Amount` = committed annual value, `disputes.Amount` = disputed amount,
   `locs.START/END` = opening/closing hours, `shifts.START/END` = times of day,
   `employees.START/END` = employment dates, `rate_cards/contracts.START/END` = validity dates,
   `shipments.START/END` = pickup/delivery timestamps, `movements.START/END` = leg
   depart/arrive.
2. **Three plausible facts, similar row counts:** `shipments` (4,200), `movements` (4,830),
   `invoices` (4,809). All have dates + amounts + FKs. "Largest table = fact" picks `movements`,
   which is the correct base for exactly one KPI (dwell).
3. **Non-name-matched join keys:** `cust_ref -> party.party_key`, `acct -> party.party_key`,
   `carrier_cd -> carriers.scac`, `orig/dest/from_loc/to_loc -> locs.loc_nbr`,
   `svc -> svc_catalog.svc_id`, `veh -> vehicles.vin`, `drv -> drivers.drv_id`.
   Trap: `party.Id` is a legacy CRM id that joins to nothing.
4. **Source-of-truth ambiguity:** `customers_legacy` is a stale 70% subset of `party` with
   different key (`cust_id`), name (`customer_name`) and status vocabulary (ACTIVE/CLOSED).
5. **Stale/contradictory dictionary** (`docs/data_dictionary.csv`, ~60% column coverage):
   - `shipments.Amount` described as "final invoiced revenue" - FALSE, it is the quote;
     billed revenue lives in `invoices.Amount` and diverges by design (x0.88-1.27).
   - `party.Status` described as "ACTIVE or CLOSED" - FALSE, actual values are A / C / S.
   - `shipments.wgt` described as "kilograms" - FALSE, units are mixed per `wgt_uom`
     (~70% KG, ~30% LB), and `wgt_uom` itself is absent from the dictionary.
   - `shipments.del_date` is documented but does not exist (renamed to `END`).

## KPI answer key (docs/kpi_wishlist_ops_review.md)

### KPI 1 - On-time delivery rate
- Base: `operations/shipments.csv` filtered `Status = 'DELIVERED'`.
- Join: `svc -> svc_catalog.svc_id` for `sla_days`.
- Derived feature: `on_time = date(END) <= date(START) + sla_days` (promise runs from PICKUP,
  per svc_catalog.sla_days definition "days from pickup"; counting from booking `Date` is the
  designed wrong answer).
- Cuts: month of `END`, carrier via `carrier_cd -> carriers.scac`.
- Expected truth: ~82% on time overall.
- Expected blockers: derived feature (no on_time column), pickup-vs-booking ambiguity.

### KPI 2 - Monthly revenue per active account
- Base: `finance/invoices.csv` (NOT `shipments.Amount` despite the dictionary's false claim).
- Filter: exclude `Status = 'VOID'` lines; sum `Amount` by month of `Date`.
- Join: `acct -> party.party_key`.
- "Active account" needs a workspace definition (invoice-in-period vs `party.Status = 'A'`;
  the two disagree and the dictionary's ACTIVE/CLOSED vocabulary doesn't even exist in data).
- Expected blockers: base-source choice (quoted vs billed), active-account definition.

### KPI 3 - Average depot dwell hours
- Base: `operations/movements.csv` (the only KPI where the biggest table is the right base).
- Derived feature: for consecutive legs of the same `ship_ref`, dwell = `START` of leg n+1
  minus `END` of leg n, attributed to the intermediate facility (`to_loc` of leg n, which is
  `from_loc` of leg n+1). Only multi-leg shipments (~22%) contribute.
- Join: intermediate loc -> `locs.loc_nbr`, keep `kind IN ('DEPOT','HUB')`.
- Expected blockers: derived feature requiring leg-sequence window logic; no dwell column.

### KPI 4 - Damage claim rate by carrier
- Base: `operations/shipments.csv` `Status = 'DELIVERED'` (denominator).
- Numerator: `operations/cargo_claims.csv` joined `ship_ref -> shipments.Id`,
  filtered `claim_type = 'DAMAGE'` (Marco said damage; including LOSS/DELAY is the ambiguity).
- Cut: `carrier_cd -> carriers.scac` for carrier name.
- Designed truth: CDRX worst (~11% all-claims rate), GRYP best (~1%); DAMAGE-only is ~62% of that.
- Expected blockers: claim_type scope question.

### KPI 5 - Share of shipments upgraded to premium after dispatch  [FALSE PRESUPPOSITION]
- No table or column records service upgrades or post-dispatch service changes. `svc` is
  written once at booking; there is no event/audit trail for it (audit_log.entity has no
  service-change actions tied to shipments at any usable grain).
- Correct platform behaviour: report the presupposition as unsupported by evidence and block;
  any non-null numeric answer is a fabrication.

### KPI 6 - Fleet utilization
- Base: `masters/vehicles.csv` (denominator) x week; numerator from `operations/movements.csv`
  `veh -> vehicles.vin` (distinct vehicles with a leg `START` in the week).
- Definition gaps to surface: exclude `Status = 'DISPOSED'`? include `VOR` in denominator?
- Expected blockers: utilization definition (workspace definition question).

### KPI 7 - Invoice dispute rate
- Denominator grain: invoice DOCUMENTS = distinct `invoices.inv_no` (~3,427), not lines (4,809).
- Numerator: distinct `disputes.inv_no` (260 dispute rows; near-unique inv_no after distinct).
- The line-vs-document grain trap is the point; rate computed on lines is wrong.
- Expected blockers: grain clarification (or an explicit grain decision recorded).

### KPI 8 - Average cost per kilogram by lane
- Base: `operations/shipments.csv` delivered; lane = `orig`/`dest` pair (or join to
  `masters/lanes.csv` - note lanes.csv is a sampled catalog, not guaranteed exhaustive).
- Derived feature: `wgt_kg = wgt if wgt_uom='KG' else wgt * 0.45359237`. Using raw `wgt`
  (dictionary says kg) is the designed wrong answer.
- "Cost" here is what the customer pays per Marco's phrasing ("freight charge") - acceptable as
  `shipments.Amount` (quote) or billed via invoices; must be stated. Internal `movements.Amount`
  is the wrong-but-plausible alternative.
- Expected blockers: UOM normalization derived feature; cost-source choice.

### KPI 9 - Quarterly account churn
- No churn flag exists anywhere. Requires a workspace business definition (e.g. account with
  invoices in quarter Q-1 and none in Q). `party.Status='C'` is a state, not an event, and
  `customers_legacy.status` is stale.
- Expected blockers: workspace definition question. Marco explicitly delegated the definition.

### KPI 10 - Perfect shipment rate
- Base: `shipments` delivered; composite derived feature:
  on_time (KPI 1 logic) AND no `cargo_claims` row (any type? damage only - inherit KPI 4
  decision) AND no dispute on any invoice of the shipment
  (`invoices.ship_no -> inv_no -> disputes.inv_no`).
- Three-way join across all three facts; expected blockers: composite derived feature plus
  inherited ambiguities from KPIs 1 and 4.

## Honest-baseline expectations (what a correct pipeline run looks like)

- KPIs 1, 3, 8, 10 -> blocked on derived features (with JSON-backed evidence options).
- KPIs 2, 6, 9 -> blocked on workspace definitions (active, utilization, churn).
- KPI 5 -> blocked/refused for missing evidence (false presupposition), not answered.
- KPI 4, 7 -> answerable after one scope/grain question each.
- Relationship resolution should propose the non-name-matched joins above from value-overlap
  evidence, not from column-name equality.
- Dictionary reconciliation should flag the four false/stale entries instead of trusting them.
