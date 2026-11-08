# contracts/fixtures

Fictional, fully consistent test data for every part of the build. Regenerate with `npm run fixtures`
(`python3 fixtures/generate.py`, needs Pillow, output is deterministic). Check with `npm run validate`.
Never edit the JSON by hand: change `generate.py` and regenerate, so images, OCR, fields and findings stay in sync.

## Files

| Path | Content | Used by |
| --- | --- | --- |
| `pages/<docId>-page-N.png` | Canonical page render (1240 x 1754 px, 150 dpi). Every box below lands on visible text | frontend viewer, render route, dossier crops |
| `originals/<docId>.png/.pdf` | The "uploaded original"; its SHA-256 is `DocumentRecord.sha256` (`D-03.pdf` is an image-only PDF) | integrity check, mock upload |
| `ocr/<docId>-page-N.json` | Recorded `OcrPage` for each page, boxes match the images | Python batches (extractor, layout, text layer) |
| `analyze/<docId>.json` | Recorded `AnalyzeResponse` (pages, fields, signals, analyzer statuses) | backend mock `AnalysisClient`, reasoning tests |
| `CASE-00x.reasoning.json` | Complete expected `ReasoningOutput` | backend mock reasoning, frontend mocks, B7b test |
| `CASE-00x.case.json` | `CaseFixture`: case, documents, jobs, investigation log | backend and frontend mocks |
| `CASE-00x.graph.json` | `GraphResponse` | entity graph |
| `expected_findings.json`, `expected_non_findings.json` | What the engine must and must not produce, per case | B6 and B7b tests, evaluation |

## Cases

**CASE-001** (documents `D-01` id_card, `D-02` application_form, `D-03` bank_statement, one applicant, one account holder)

| Expected | What |
| --- | --- |
| `DOB_CONFLICT` high (F-0001) | D-01 reads 2004-08-12, D-02 reads 2001-08-12, same RESOLVED applicant (id_number match) |
| `NAME_VARIANT_SPELLING` low (F-0002) | "Aarav Kumar Sharma" vs "Aarav Sharma", token-sort ratio 80 |
| `NAME_VARIANT_SPELLING` low, capped 0.3 (F-0003) | D-02 vs D-03 name; the account holder E-002 is only a SUGGESTED link to the applicant, so confidence is capped and says so |
| `STATEMENT_TOTAL_MISMATCH` medium (F-0004) | printed total 49,562.75, listed deposits sum to 48,562.75 |
| `REGION_EVENT` medium (F-0005) | D-02 `image.ela` + `image.noise` overlap on the requested amount: ONE event |
| `TEXT_LAYER_MISMATCH` medium (F-0006) | D-03 rendered total differs from the text layer |
| **No finding** | permanent vs current address (MAY_DIFFER), requested amount vs account balance (NOT_COMPARABLE), equal id_number, equal permanent address, D-01 vs D-03 name (ratio 100) |

`D-02`'s `signature` analyzer is `UNAVAILABLE`: its job is `PARTIAL` and coverage is below 1, but the priority band is not lowered.

**CASE-002** (`D-11` and `D-12` id cards, `D-13` form): applicant "Priya Nair" and guarantor "Priya Nayar" have similar names and
different id_numbers. They stay two RESOLVED entities; there are no findings at all (priority `low`).

## Conventions the engine must follow (these fix what the batch prompts leave open)

**Field names** (`ExtractedField.name`): `full_name`, `dob`, `id_number`, `address_permanent`, `address_current`, `issue_date`,
`application_date`, `requested_amount`, `account_number`, `account_balance`, `statement_date`, `statement_total`, `txn_amount` (repeats),
and `guarantor_full_name`, `guarantor_id_number`. A `guarantor_` prefix means the field is about the guarantor: strip it for
`semanticType`, and the provisional `entityRole` is `guarantor`.

**IDs.** Field and signal ids only have to be unique within their document (the Python service numbers `FLD-001`, `SIG-001` per document); the engine derives case-unique assertion ids (`<docId>-A<nn>` when the field id is `<docId>-F<nn>`, else `A-<docId>-<fieldId>`). The fixtures use the document-prefixed form: `fieldId = <docId>-F<nn>`, `assertionId = <docId>-A<nn>` (same `nn` as the field), signals `<docId>-S<nn>`, entities `E-001...`
numbered by first assertion in input order, findings `F-0001...` (rule findings in rule-table order, then events), evidence `E-0001...`,
events `EV-001...`. Tests should match findings by `type` plus evidence assertion ids, not by finding id.

**Entities.** Parties (assertions of one document with the same provisional role) merge by exact `id_number`, then `account_number`; the merged
entity is `RESOLVED` and `basis` reads `id_number exact match: <value> (<docIds>)`. A merge is refused if it would join two different
id_numbers or two different specific roles (applicant, co_applicant, guarantor, nominee). Resolved assertions take the entity's role.
Name similarity never merges: a party with no cross-document identifier match that resembles exactly one compatible entity (token-sort >= 92,
no conflicting id_number) is `SUGGESTED` and carries `suggestedLinks`; several candidates or none leave it `UNRESOLVED`.
Entities with different id_numbers and similar names (ratio >= 75) get a note in `basis`.

**Comparison scope.** MUST_AGREE rules compare assertions of the same entity, and across a SUGGESTED link with confidence capped at 0.3.
Different RESOLVED entities are never compared. ARITHMETIC rules work inside one document and are not entity-dependent.

**Confidence.** Pair findings: `min(extractionConfidence of both assertions)`, capped at 0.3 across a non-RESOLVED link. Arithmetic: min over all involved assertions.
Events: max member confidence + 0.05 when a second distinct detector family agrees (cap 1).

**Event to finding.** An event with two or more signals becomes `REGION_EVENT`. An event with one signal keeps the signal's type
(`TEXT_LAYER_MISMATCH`). Severity from event confidence: below 0.4 low, below 0.75 medium, else high. Status `POTENTIAL_ANOMALY`.

**Rule ids used in evidence** (`measurements.ruleId`, must exist in `backend/config/comparability.json`): `R-DOB-MUST-AGREE`,
`R-NAME-MUST-AGREE`, `R-IDNUM-MUST-AGREE`, `R-ADDR-MUST-AGREE`, `R-ADDR-MAY-DIFFER`, `R-AMOUNT-NOT-COMPARABLE`, `R-STMT-TOTAL-ARITH`, `R-ID-UNIQUENESS`
(also `R-TEMP-DOB-BEFORE-ISSUE`, `R-TEMP-DOB-BEFORE-APPLICATION`, `R-ACCT-UNIQUENESS`; the table is `backend/config/comparability.json`).

**Priority (reference calculation in `generate.py`).** Term per finding = `severityWeight * confidence`; noisy-OR within a category;
categories with only info/low findings capped at 0.49; across categories noisy-OR of `categoryWeight * categoryScore`
(cross_document 1.0, context 0.9, visual 0.8, pdf_structure 0.7, extraction 0.4); bands low < .25, moderate < .5, high < .75, else critical.
Confidence = severity-weighted mean of contributing finding confidences (0 when none). Coverage = AVAILABLE / (AVAILABLE + UNAVAILABLE + ERROR)
analyzers, NOT_APPLICABLE excluded. A per-document result uses findings whose evidence touches that document.
Coverage never lowers the band. All weights are echoed in `priority.provenance.config`.

**Expected files.** A finding satisfies an `expected_findings` row when `type` matches and its evidence contains all `assertionIds`
(or its event contains all `signalIds`). A finding violates an `expected_non_findings` row when its evidence contains all `assertionIds`
and its `type` is in `forbiddenTypes` (`"*"` means any type). `validate.ts` checks the recorded reasoning output against both; the B6 and
B7b tests must run the same check against the real engine output.

## Honesty notes
- Signals in `analyze/*.json` are recorded values, not recomputed. The ELA and noise cues in `D-02` are not real detector output;
  the tinted, noisy background behind the amount is only a visual cue.
- `D-03.pdf` has no text layer (it is an image PDF), so `TEXT_LAYER_MISMATCH` there is a recorded claim.
- Confidence and priority numbers are untuned starting values, validated only against this synthetic set.
