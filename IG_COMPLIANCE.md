# INDIAcore / ABDM FHIR IG Compliance Check (AF-05)

A self-assessment of the resources in `resources/` against the official
**FHIR Implementation Guide for ABDM** published by NRCES (National Resource
Centre for EHR Standards, CDAC) — the de-facto "INDIAcore" IG for India's
Ayushman Bharat Digital Mission. Assessed against v6.5.0 / v7.0.0 (FHIR R4).

> Scope note: per the PRD, full IG conformance is **out of MVP scope**. This
> document reviews where we already align, where we don't, and the exact fix —
> which is the deliverable AF-05 actually asks for.

---

## 1. The single biggest gap: document Bundle vs transaction Bundle

ABDM does **not** exchange loose resources. Clinical data moves as a
**`DocumentBundle`** (`Bundle.type = "document"`) whose **first entry is a
`Composition`** acting as the signed, human-readable clinical document. The IG
defines eight Composition profiles:

`OPConsultRecord` · `DiagnosticReportRecord` · `PrescriptionRecord` ·
`DischargeSummaryRecord` · `WellnessRecord` · `ImmunizationRecord` ·
`HealthDocumentRecord` · `InvoiceRecord`.

| Our project | ABDM IG |
|---|---|
| Individual resources POSTed (`validate_resources.py`) | Not how ABDM exchanges data |
| `transaction` Bundle (`submit_bundle.py`, AF-02) | Needs to be a **`document`** Bundle |
| No `Composition` | **Composition is mandatory** and must be entry[0] |

**Our scenario maps cleanly to two ABDM documents:**
- The two OPD visits → **`OPConsultRecord`** Composition(s).
- The admission → **`DischargeSummaryRecord`** Composition.

**Fix:** wrap the existing resources in a `document` Bundle led by an
`OPConsultRecord`/`DischargeSummaryRecord` Composition that `section`-references
the Conditions, Observations and MedicationRequests. All our resource bodies are
reusable as-is inside that Composition.

---

## 2. Resource-by-resource compliance

Legend: ✅ meets IG · ⚠️ partial · ❌ gap

### Patient — ⚠️ partial
ABDM Patient profile (`https://nrces.in/ndhm/fhir/r4/StructureDefinition/Patient`)
mandates: **`identifier` (1..*), `identifier.type` (1..1), `identifier.value`
(1..1)**; must-support `identifier.system`, `name.text`, `telecom.system/value`,
`gender`, `birthDate`, `address`.

| Element | Ours | Verdict |
|---|---|---|
| `identifier.value` (ABHA) | present | ✅ |
| `identifier.system` | `https://abdm.gov.in/ABHA` | ✅ (must-support) |
| **`identifier.type`** | **missing** | ❌ **mandatory** |
| `name.text`, `gender`, `birthDate`, `telecom`, `address` | all present | ✅ |

**Fix:** add `identifier.type` bound to the IG value set
`ndhm-identifier-type-code` (extensible). For an ABHA number, e.g.:
```json
"type": { "coding": [{ "system": "https://nrces.in/ndhm/fhir/r4/ValueSet/ndhm-identifier-type-code",
                       "code": "ABHA", "display": "Ayushman Bharat Health Account" }] }
```
Also note: the canonical ABDM ABHA-number system in production is the
NDHM/ABDM-published URI; the PRD's `https://abdm.gov.in/ABHA` is a teaching
placeholder. Real submissions should use the official system URI.

### Practitioner — ⚠️ partial
We include an NMC registration `identifier` + `qualification` + `name`. The IG
Practitioner profile similarly wants `identifier.type`. **Fix:** add
`identifier.type` (e.g. a "Medical License number" type code).

### Encounter — ✅ largely compliant
`status`, `class` (AMB/IMP via v3-ActCode), `subject`, `participant`, `period`,
`hospitalization` (inpatient) all present and conformant.

### Condition — ⚠️ partial (terminology)
Structurally complete (`clinicalStatus`, `verificationStatus`, `category`,
`code`, `subject`, `encounter`, `onset`). **The IG prefers SNOMED CT** for
problems/diagnoses. We used **ICD-10** (as the PRD requires). ICD-10 is accepted
for billing/reporting, but a fully IG-aligned Condition would carry a SNOMED CT
coding (optionally alongside ICD-10 as a second `coding`).
**Fix:** add a SNOMED CT `coding` to `code.coding[]` (e.g. T2DM `44054006`,
essential hypertension `59621000`).

### Observation — ✅ compliant
`status`, `category`, `code` (LOINC — IG-preferred for labs/vitals), `subject`,
`encounter`, `effectiveDateTime`, `valueQuantity` with UCUM units. Matches the
IG's WellnessRecord/DiagnosticReportRecord expectations. (Optional polish: model
BP as a single Observation with systolic/diastolic `component`s per the FHIR
vital-signs profile; we kept them separate as the PRD specifies.)

### MedicationRequest — ⚠️ partial (terminology)
Complete structure (`status`, `intent`, `medicationCodeableConcept`, `subject`,
`encounter`, `authoredOn`, `requester`, `dosageInstruction`). We used **RxNorm**;
the IG prefers **SNOMED CT** or India-specific drug codes for `medication`.
**Fix:** add a SNOMED CT coding for the drug, or keep RxNorm as a secondary code.

### AllergyIntolerance — ✅ compliant
`clinicalStatus`, `verificationStatus`, `type`, `category`, `criticality`,
`code` (SNOMED CT — IG-preferred), `patient`, `reaction`. Good alignment.

### Provenance — ✅ supports ABDM audit needs
ABDM requires audit/traceability; our Provenance (agent + activity + target +
recorded) directly supports this. Production ABDM additionally signs documents
(`Bundle.signature` / digital signature on the document Bundle) — see §3.

### Questionnaire / QuestionnaireResponse — ✅ valid, ⚠️ not an ABDM record type
Valid R4 and useful for structured intake, but the ABDM HDE record set does not
define a dedicated intake-questionnaire document; in ABDM this content typically
appears within an `OPConsultRecord`. No fix needed for correctness.

---

## 3. Cross-cutting gaps

| # | IG expectation | Our status | Fix |
|---|---|---|---|
| G1 | `meta.profile` asserts the ABDM profile canonical on each resource | not asserted | add `meta.profile` (e.g. `.../StructureDefinition/Patient`) |
| G2 | `identifier.type` on Patient/Practitioner | missing | add `type` from `ndhm-identifier-type-code` |
| G3 | Exchange as `document` Bundle + `Composition` | we use transaction Bundle | add Composition + switch Bundle type (see §1) |
| G4 | SNOMED CT preferred for Condition/Medication | ICD-10/RxNorm used | add SNOMED CT codings |
| G5 | Document digital signature (`Bundle.signature`) | none | sign the document Bundle |
| G6 | Official ABHA system URI | placeholder used | use NDHM/ABDM-published system URI |
| G7 | SMART-on-FHIR / ABHA-based consented access (ABDM HIE-CM flow) | open sandbox | out of scope (AF-03) |

---

## 4. Summary scorecard

| Area | Verdict |
|---|---|
| Resource structure & required fields | ✅ Strong — all resources are valid R4 and HAPI-accepted |
| Linking / references | ✅ Strong — no dangling references |
| ABDM Patient `identifier.type` | ❌ Must add (mandatory) |
| Terminology (SNOMED CT preference) | ⚠️ ICD-10/RxNorm used; add SNOMED CT codings |
| Document Bundle + Composition | ❌ Largest structural gap |
| Profile assertion (`meta.profile`) | ❌ Not asserted |
| Audit trail (Provenance) | ✅ Present |

**Bottom line:** the resources are clean, valid FHIR R4 that submit successfully,
and they are ~70% of the way to ABDM IG conformance. The remaining work is
well-defined: assert profiles, add `identifier.type`, add SNOMED CT codings, and
wrap everything in a signed `document` Bundle led by an `OPConsultRecord` /
`DischargeSummaryRecord` Composition.

---

## Sources
- [FHIR Implementation Guide for ABDM — Profiles](https://nrces.in/ndhm/fhir/r4/profiles.html)
- [ABDM Patient profile (StructureDefinition)](https://www.nrces.in/ndhm/fhir/r4/StructureDefinition-Patient.html)
- [Identifier Type value set](https://www.nrces.in/preview/ndhm/fhir/r4/ValueSet-ndhm-identifier-type-code.html)
- [Implementation Guide for Adoption of FHIR in ABDM and NHCX (PDF)](https://www.nrces.in/download/files/pdf/Implementation_Guide_for_Adoption_of_FHIR_in_ABDM_and_NHCX.pdf)
