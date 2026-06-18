# FHIR Resource Modelling — Clinical Timeline Builder

Mini Project 3 · Module 3 (Interoperability & Health Data APIs) · PES University

A complete, validated set of **FHIR R4** resources modelling a real diabetic
follow-up care pathway, plus two Python tools: one that submits the resources to
a HAPI FHIR server and produces a validation report, and one that fetches them
back and reconstructs a chronological clinical timeline.

---

## Clinical scenario

**Patient:** Rajesh Sharma, 52-year-old male, Type 2 Diabetic (Karnataka)
**ABHA ID:** `1234-5678-9012-3456`
**Treating clinician:** Dr. Anil Gupta, AIIMS Endocrinology

| When (2026) | Event |
|---|---|
| **13 Mar** | Initial OPD visit — polyuria & fatigue, BP 145/90. Diagnosed T2DM (E11.9) + Hypertension (I10). Labs: HbA1c 9.2 %, FBG 185 mg/dL, Creatinine 1.1 mg/dL. Started Metformin 500 mg BD + Amlodipine 5 mg OD. |
| **24 Apr** | OPD follow-up — BP improved to 128/82, FBG 142 mg/dL. |
| **30 May → 01 Jun** | Inpatient admission — acute hyperglycaemia (BG 380 mg/dL). Regular insulin started. 3-day stay, discharged home. |

> Dates are anchored to the project date (2026-06-13) per the PRD's relative
> timeline ("3 months ago", "6 weeks later", "2 weeks ago").

---

## Project layout

```
.
├── resources/                      # 20 hand-authored FHIR R4 JSON files
│   ├── patient.json                # Patient (ABHA identifier)
│   ├── practitioner.json           # Practitioner (NMC registration)
│   ├── encounter_opd.json          # Encounter — AMB (OPD)
│   ├── encounter_followup.json     # Encounter — AMB (follow-up)
│   ├── encounter_inpatient.json    # Encounter — IMP (admission)
│   ├── observation_hba1c.json      # LOINC 4548-4
│   ├── observation_fbg_1.json      # LOINC 1558-6
│   ├── observation_fbg_2.json      # LOINC 1558-6
│   ├── observation_bp_sys_1.json   # LOINC 8480-6
│   ├── observation_bp_dia_1.json   # LOINC 8462-4
│   ├── observation_bp_sys_2.json   # LOINC 8480-6
│   ├── observation_bp_dia_2.json   # LOINC 8462-4
│   ├── observation_creatinine.json # LOINC 2160-0
│   ├── observation_bg_admission.json # LOINC 2339-0
│   ├── condition_diabetes.json     # ICD-10 E11.9
│   ├── condition_hypertension.json # ICD-10 I10
│   ├── medication_metformin.json   # MedicationRequest (RxNorm)
│   ├── medication_amlodipine.json  # MedicationRequest (RxNorm)
│   ├── medication_insulin.json     # MedicationRequest (RxNorm)
│   ├── allergy.json                # AllergyIntolerance (sulfonamide)
│   ├── provenance_*.json           # AF-01: audit trail (one per encounter)
│   ├── questionnaire_diabetes_intake.json    # AF-04: intake form
│   └── questionnaire_response_intake.json    # AF-04: filled answers
├── validate_resources.py           # F-02: upsert to HAPI + validation report
├── timeline_builder.py             # F-03: fetch + chronological timeline
├── submit_bundle.py                # AF-02: one-call transaction Bundle
├── cleanup_sandbox.py              # purge duplicate identities from the sandbox
├── fhir_ai.py                      # GA-01..05: LLM-powered helpers
├── IG_COMPLIANCE.md                # AF-05: ABDM/NRCES IG compliance check
├── build_report.py                 # generate the Word project report (.docx)
├── run_pipeline.bat                # one-click: validate -> timeline -> report
├── requirements.txt                # requests, anthropic, python-docx
└── output/                         # generated artifacts (see below)
    ├── validation_report.txt       # per-resource HTTP status / id / issues
    ├── id_map.json                  # local id -> server id map
    ├── transaction_bundle.json      # the assembled AF-02 Bundle
    ├── bundle_report.txt            # AF-02 per-entry results
    ├── patient_timeline.json        # structured timeline export
    └── Project3_FHIR_Report.docx    # consolidated Word project report
```

**Resource types covered (10):** Patient, Practitioner, Encounter, Observation,
Condition, MedicationRequest, AllergyIntolerance, Provenance, Questionnaire,
QuestionnaireResponse.

---

## Setup

```bash
python -m venv .venv          # optional
pip install -r requirements.txt
```

Requires Python 3.9+ (developed on 3.12). Only third-party dependency is `requests`.

---

## Usage

### 1. Validate & submit resources to HAPI (F-02)

```bash
# Dry run first — rewrites references and writes a report WITHOUT any network call:
python validate_resources.py --dry-run

# Live submission to the public HAPI R4 sandbox:
python validate_resources.py
```

What it does:
- Loads every file in `resources/` in **dependency order** (Patient →
  Practitioner → Encounter → Observation/Condition/MedicationRequest/Allergy).
- Submits each resource with a **conditional update** (upsert) keyed on its stable
  identifier, and rewrites local references such as `Patient/rajesh-sharma` to the
  **server-assigned ids** — so nothing is left dangling once HAPI renumbers them.
- Records HTTP status, the `Location` header, the server id, and parses any
  `OperationOutcome` issues into **`output/validation_report.txt`**.
- Saves the id map (including the patient's server id) to **`output/id_map.json`**.

**Idempotent by design (one ABHA = one Patient).** Each resource carries a stable
business identifier (Patient → ABHA, Practitioner → NMC, everything else → a
`urn:fhir-modelling:resource-key`; Provenance uses a stable `meta.tag`). Submission
is a FHIR **conditional update** — `PUT {Type}?identifier=…` (or `?_tag=…` for
Provenance) — which **creates** the resource if none matches and **updates it in
place** if one does. Re-running therefore updates the same logical resources
instead of duplicating them, so an `identifier` search always resolves to a single
patient — the behaviour a real ABDM integration requires.

> **Identity note.** This replaces an earlier "create a fresh copy every run"
> workaround, which kept the demo green but left many Patients sharing one ABHA —
> wrong for real data. If a shared sandbox already holds such duplicates, run
> `python cleanup_sandbox.py` once to remove them (see below).

Options: `--base-url <url>`, `--timeout`, `--delay`, and `--fresh` (throwaway-demo
mode: POST a brand-new, per-run-unique copy of every resource instead of upserting
— fine for a one-off sandbox demo, but it leaves duplicate identities behind).

#### Cleaning up duplicate identities

```bash
python cleanup_sandbox.py --dry-run     # list duplicate Patients/Practitioners
python cleanup_sandbox.py               # cascade-delete them from the sandbox
```

Removes every Patient with the scenario ABHA and every Practitioner with the
scenario NMC number (cascading to their referencing resources), so the next upsert
run starts from a single, clean identity.

### 2. Build the clinical timeline (F-03)

```bash
# Easiest — reuse the patient id captured during validation:
python timeline_builder.py auto

# Or pass an id explicitly:
python timeline_builder.py 1234-5678-9012-3456      # by ABHA id (identifier search)
python timeline_builder.py 99001                    # by HAPI server id

# Offline — build straight from the local files (no server needed):
python timeline_builder.py --offline-dir resources
```

It fetches the patient's resources via FHIR search
(`Patient?identifier=`, `Encounter?patient=`, `Observation?patient=&_sort=date`,
`Condition?patient=`, `MedicationRequest?patient=`), merges them into a single
chronological list grouped by encounter, prints the timeline, and writes
**`output/patient_timeline.json`**.

Sample console output:

```
-- 2026-03-13  Initial OPD visit - Endocrinology [Ambulatory / OPD] (Dr. Anil Gupta)
     Conditions : Type 2 Diabetes Mellitus (E11.9), Essential Hypertension (I10)
     Labs       : HbA1c 9.2 %, Fasting Blood Glucose 185 mg/dL, Serum Creatinine 1.1 mg/dL
     Vitals     : Systolic Blood Pressure 145 mmHg, Diastolic Blood Pressure 90 mmHg
     Medications: Metformin 500 mg oral tablet [...BD...], Amlodipine 5 mg oral tablet [...OD...]
-- 2026-04-24  OPD follow-up visit - Endocrinology [Ambulatory / OPD] (Dr. Anil Gupta)
     ...
-- 2026-05-30  Inpatient admission - acute hyperglycaemia [Inpatient] (Dr. Anil Gupta)
     ...
-- 2026-06-01  Discharge  (Inpatient admission - acute hyperglycaemia)
```

---

## Recommended run order

```bash
pip install -r requirements.txt
python validate_resources.py --dry-run      # sanity-check linking offline
python validate_resources.py                # submit to HAPI -> validation_report.txt + id_map.json
python timeline_builder.py auto             # fetch + build -> patient_timeline.json
python build_report.py                      # -> output/Project3_FHIR_Report.docx
```

On Windows you can just double-click **`run_pipeline.bat`**, which runs all three
steps (validate → timeline → report) in order.

### 3. Build the Word project report

```bash
python build_report.py        # writes output/Project3_FHIR_Report.docx
```

`build_report.py` assembles a single submittable Word document covering the whole
project: scenario, architecture, the full resource inventory, validation evidence
(read live from `validation_report.txt` and `bundle_report.txt`), the
reconstructed timeline, the GenAI/bonus features, and the IG-compliance summary.
The evidence tables are read at generation time, so run the pipeline first and the
report reflects the latest real results.

> **HAPI sandbox caveat:** `https://hapi.fhir.org/baseR4` is a free public
> server and is occasionally slow or down, and old test data is periodically
> purged. Run validation **well before** any deadline and keep the generated
> `validation_report.txt`. The offline modes (`--dry-run`,
> `--offline-dir resources`) let you demonstrate the full pipeline without the
> network.

The `output/` files committed here were produced by the offline/dry-run modes as
samples; a live `validate_resources.py` run overwrites them with real server data.

---

## Coding systems used

| Concept | System | Example code |
|---|---|---|
| ABHA identifier | `https://abdm.gov.in/ABHA` | 1234-5678-9012-3456 |
| Lab / vital codes | LOINC `http://loinc.org` | 4548-4 (HbA1c) |
| Diagnoses | ICD-10 `http://hl7.org/fhir/sid/icd-10` | E11.9, I10 |
| Medications | RxNorm `http://www.nlm.nih.gov/research/umls/rxnorm` | 6809 (Metformin) |
| Units | UCUM `http://unitsofmeasure.org` | mg/dL, mm[Hg], % |
| Encounter class | v3-ActCode | AMB, IMP |

---

## Bonus features

### AF-02 · Transaction Bundle (one-call submission)

```bash
python submit_bundle.py --dry-run     # build output/transaction_bundle.json offline
python submit_bundle.py               # POST all resources atomically in one call
```

Assembles a FHIR `transaction` Bundle: each resource gets a `urn:uuid` and all
references are rewritten to those placeholders, which the server resolves
atomically. By default every entry is a **conditional update** (`PUT
{Type}?identifier=…`, or `?_tag=…` for Provenance) on the resource's stable
identifier, so re-submitting the Bundle **updates the same resources in place**
rather than creating duplicates — consistent with `validate_resources.py`. Pass
`--fresh` for the throwaway behaviour (a unique per-run `POST` of every resource).
Results land in `output/bundle_report.txt`.

### GA-01..05 · LLM-powered helpers (`fhir_ai.py`)

Needs the `anthropic` package and `ANTHROPIC_API_KEY` (defaults to
`claude-sonnet-4-6`):

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."        # PowerShell: $env:ANTHROPIC_API_KEY = "..."

python fhir_ai.py loinc --test "blood HbA1c"                     # GA-04
python fhir_ai.py generate --note "HbA1c 9.2%, FBG 185 mg/dL" --patient-id rajesh-sharma  # GA-01
python fhir_ai.py narrative --timeline output/patient_timeline.json --format referral_letter  # GA-02
python fhir_ai.py explain --outcome examples/operation_outcome.json --resource resources/observation_hba1c.json  # GA-03
python fhir_ai.py diff --before output/timeline_v1.json --after output/patient_timeline.json   # GA-05
```

> LLM output can be syntactically valid but clinically wrong (PRD risk table).
> Always validate generated resources against HAPI before trusting them.

### AF-05 · IG compliance

See **`IG_COMPLIANCE.md`** for a self-assessment against the ABDM/NRCES India
FHIR R4 Implementation Guide (satisfied constraints, gaps, and fixes).

---

## Scope

**Built — MVP (F-01 → F-03):** all resource JSON, HAPI validation with reference
rewriting + report, and the timeline builder with JSON export.

**Built — bonus:** AF-01 Provenance, AF-02 transaction Bundle, AF-04
Questionnaire/QuestionnaireResponse, AF-05 IG compliance report, and all GenAI
features GA-01–05 (`fhir_ai.py`).

**Out of scope:** AF-03 SMART-on-FHIR / OAuth mock (the PRD lists OAuth as
out-of-scope; the open HAPI sandbox is used).
