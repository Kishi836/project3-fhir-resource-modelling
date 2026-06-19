# Project Plan — FHIR Resource Modelling (Mini Project 3)

> Clinical Timeline Builder with FHIR R4 Validation · PES University · Module 3

Single source of truth for what was planned, what is complete, and what remains.

---

## Goal

Build the deliverable defined in `PRD_P3_FHIR_Resource_Modelling.md`: a hand-authored
set of FHIR R4 resources for a fixed diabetic care pathway, a script that POSTs them
to a HAPI FHIR server and produces a validation report, and a script that fetches them
back and reconstructs a chronological timeline — plus a chosen set of bonus features.

Grading weight: FHIR JSON validity 40% · Timeline builder 35% · Report 25%.

## Confirmed decisions

- **Scope:** MVP (F-01 → F-03) **plus** bonus features AF-01, AF-02, AF-04, AF-05 and
  GenAI GA-01..05. Only **AF-03 (SMART/OAuth mock)** is out of scope.
- **Validation:** scripts run against the live public HAPI sandbox. Live demos were
  performed during the build; the canonical "submit before deadline" run is owned by
  the team (the sandbox purges test data periodically).
- **GenAI key:** `fhir_ai.py` is built to read `ANTHROPIC_API_KEY` from the environment
  and run later; no live LLM demo was performed during the build (user's choice).
- **Default LLM model:** `claude-sonnet-4-6`.
- **Dates anchored to 2026-06-13** (relative-date intent from the PRD):
  - OPD visit + labs + meds: **2026-03-13**
  - Follow-up (+6 weeks): **2026-04-24**
  - Inpatient admission (3-day stay): **2026-05-30 → 2026-06-01**

---

## Task board

| # | Task | Status |
|---|---|---|
| 1 | Project skeleton (folders, requirements.txt) | ✅ Done |
| 2 | Author FHIR resource JSON files | ✅ Done |
| 3 | `validate_resources.py` (POST + report + id map) | ✅ Done |
| 4 | `timeline_builder.py` (fetch + timeline + JSON export) | ✅ Done |
| 5 | Local validation pass (JSON, references, graceful handling) | ✅ Done |
| 6 | Finalize README + deliverables | ✅ Done |
| 7 | Live HAPI validation demo | ✅ Done — 20/20 HTTP 201 |
| 8 | AF-01 Provenance resources | ✅ Done |
| 9 | AF-04 Questionnaire + QuestionnaireResponse | ✅ Done |
| 10 | AF-02 Transaction Bundle (`submit_bundle.py`) | ✅ Done — live 25/25 |
| 11 | AF-05 INDIAcore IG compliance report | ✅ Done |
| 12 | GA-01..05 LLM features (`fhir_ai.py`) | ✅ Built (run later) |

---

## What has been built

### Resources (`resources/`, 25 files, 10 types)
Patient (ABHA), Practitioner (NMC), 3 Encounters (AMB/AMB/IMP), 9 Observations
(LOINC), 2 Conditions (ICD-10), 3 MedicationRequests (RxNorm), 1 AllergyIntolerance,
3 Provenance, 1 Questionnaire, 1 QuestionnaireResponse.
- All references use stable logical ids (`Patient/rajesh-sharma`, etc.).
- Coding systems pinned: LOINC, ICD-10, RxNorm, UCUM, v3-ActCode, condition-clinical,
  SNOMED CT (allergy/route), provenance + questionnaire terminologies.

### `validate_resources.py` (F-02)
Dependency-ordered load → reference rewrite to server ids → POST → capture
status/Location/OperationOutcome → `output/validation_report.txt` + `output/id_map.json`.
Offline `--dry-run` mode included.

### `timeline_builder.py` (F-03)
Resolve patient (ABHA id / server id / `auto`) → FHIR search fetch → chronological
merge grouped by encounter → console timeline → `output/patient_timeline.json`.
Offline `--offline-dir` mode included.

### `submit_bundle.py` (AF-02)
Assembles a FHIR `transaction` Bundle (urn:uuid fullUrls, references rewritten to those
placeholders), POSTs it in one atomic call, parses `Bundle.entry.response` for ids.
Patient/Practitioner use conditional create (`ifNoneExist`); other resources get a
per-run `meta.tag` + unique `identifier` to bypass the shared sandbox's content
de-duplication (HAPI-2840). Offline `--dry-run` mode included.

### `fhir_ai.py` (GA-01..05)
Anthropic SDK helpers — `generate` (note→Observations), `narrative` (timeline→letter),
`explain` (OperationOutcome→fix), `loinc` (test→code), `diff` (two timelines→summary).
Reads `ANTHROPIC_API_KEY`; default model `claude-sonnet-4-6`; fails gracefully without
the key/SDK.

### `IG_COMPLIANCE.md` (AF-05)
Self-assessment against the live NRCES ABDM India FHIR R4 IG — satisfied constraints,
gaps (identifier.type, SNOMED CT codings, document Bundle + Composition), and fixes.

### Verification completed
- 25/25 resource files valid JSON; 2-space pretty-printed.
- Reference integrity: 61 references checked, 0 dangling.
- Required-field spot-check: 0 problems.
- Helper functions unit-tested (reference rewrite, OperationOutcome parse, id extraction).
- Timeline ordering correct on real data; graceful (no-crash) on malformed/sparse inputs.
- All scripts compile (`py_compile`).

---

## Live HAPI demos — RESULTS (2026-06-13)

**One-by-one (`validate_resources.py`):** 20/20 resources accepted with HTTP 201, zero
OperationOutcome issues. Patient server id `136979599`. References auto-rewritten to
server ids; `timeline_builder.py auto` then fetched them back and rebuilt the timeline
(OPD → follow-up → admission → discharge).

**Transaction Bundle (`submit_bundle.py`):** HTTP 200, 25/25 entries accepted in one
atomic call — Patient/Practitioner `200 OK` (reused via conditional create), the other
23 `201 Created`.

---

## Deliverables vs PRD §13
| Deliverable | Status |
|---|---|
| `patient.json` / `practitioner.json` | ✅ |
| `encounter_opd.json`, `encounter_inpatient.json` (+ followup) | ✅ |
| `observation_*.json` (4+) | ✅ (9) |
| `condition_*.json` (2) | ✅ |
| `medication_*.json` (2) | ✅ (3) |
| `timeline_builder.py` | ✅ |
| `patient_timeline.json` (generated) | ✅ |
| `validation_report.txt` (generated) | ✅ |
| `README.md` | ✅ |
| (Optional) LLM-assisted generator | ✅ `fhir_ai.py` (GA-01..05) |

---

## What is left

1. **GA live demo (optional):** set `ANTHROPIC_API_KEY`, `pip install -r requirements.txt`,
   then run any `fhir_ai.py` subcommand (e.g. `python fhir_ai.py loinc --test "blood HbA1c"`).
2. **Team's official submission run:** re-run `validate_resources.py` ~48 h before the
   deadline and archive the report / screenshots of the 201 responses (the public
   sandbox purges test data periodically).

## Out of scope (remaining)
AF-03 SMART-on-FHIR / OAuth mock only (the PRD lists OAuth as out-of-scope; the open
HAPI sandbox is used).
