#!/usr/bin/env python3
"""GenAI / LLM features for the FHIR Resource Modelling project (PRD section 10).

Implements all five GA features using Claude via the official Anthropic SDK:

    GA-01  generate  - clinical free-text note  -> FHIR R4 Observation resources
    GA-02  narrative - patient_timeline.json    -> clinical handover narrative
    GA-03  explain   - OperationOutcome + resource -> plain-English fix
    GA-04  loinc     - lab test description     -> recommended LOINC code
    GA-05  diff       - two timelines            -> clinical change summary

Setup
-----
    pip install -r requirements.txt
    # set your key (do NOT hard-code it):
    #   PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
    #   bash:        export ANTHROPIC_API_KEY="sk-ant-..."

Usage
-----
    python fhir_ai.py loinc --test "blood HbA1c"
    python fhir_ai.py generate --note "HbA1c 9.2%, FBG 185 mg/dL on 13-Mar-2026" \
                               --patient-id rajesh-sharma --out output/llm_observations.json
    python fhir_ai.py narrative --timeline output/patient_timeline.json --format referral_letter
    python fhir_ai.py explain --outcome examples/operation_outcome.json \
                              --resource resources/observation_hba1c.json
    python fhir_ai.py diff --before output/timeline_v1.json --after output/patient_timeline.json

Model defaults to claude-sonnet-4-6 (override with --model).

Important: the LLM can produce syntactically valid but clinically wrong FHIR
(PRD Risk table). Always validate generated resources against HAPI
(`validate_resources.py`) before trusting them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None

DEFAULT_MODEL = "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# Anthropic client helpers
# --------------------------------------------------------------------------- #
def _client() -> "anthropic.Anthropic":
    if anthropic is None:
        print("ERROR: the 'anthropic' package is required. Run: pip install -r requirements.txt",
              file=sys.stderr)
        raise SystemExit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set in the environment.", file=sys.stderr)
        print("  PowerShell:  $env:ANTHROPIC_API_KEY = \"sk-ant-...\"", file=sys.stderr)
        print("  bash:        export ANTHROPIC_API_KEY=\"sk-ant-...\"", file=sys.stderr)
        raise SystemExit(1)
    return anthropic.Anthropic()


def _text_of(response: Any) -> str:
    """Return the first text block from a Messages API response."""
    for block in response.content:
        if block.type == "text":
            return block.text
    return ""


def _call(
    prompt: str,
    *,
    system: str | None = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    json_schema: dict[str, Any] | None = None,
    think: bool = False,
) -> str:
    """Single Claude call. Returns the response text (JSON string if json_schema given)."""
    client = _client()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    if think:
        kwargs["thinking"] = {"type": "adaptive"}
    if json_schema is not None:
        # Structured output: constrain the response to the schema. Don't combine
        # with adaptive thinking here — these are simple, schema-bound lookups.
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
    try:
        response = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        print("ERROR: invalid ANTHROPIC_API_KEY.", file=sys.stderr)
        raise SystemExit(1)
    except anthropic.APIError as exc:
        print(f"ERROR: Anthropic API error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if response.stop_reason == "refusal":
        print("ERROR: the model declined this request (safety refusal).", file=sys.stderr)
        raise SystemExit(1)
    return _text_of(response)


def _loads_json(text: str) -> Any:
    """Parse JSON, tolerating ```json fenced blocks the model might emit."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("`").strip()
    return json.loads(text)


# --------------------------------------------------------------------------- #
# GA-01 · FHIR Resource Generator from Clinical Notes
# --------------------------------------------------------------------------- #
def clinical_note_to_fhir(note: str, patient_id: str, model: str = DEFAULT_MODEL) -> list[dict[str, Any]]:
    """Extract lab results / vital signs from a free-text note as FHIR Observations."""
    prompt = f"""Extract every lab result and vital sign from this clinical note and format \
each as a FHIR R4 Observation resource.

Clinical note: "{note}"
Patient logical id: "{patient_id}"

For each measurement:
- Use the correct LOINC code (system "http://loinc.org") with a display name.
- Include valueQuantity (value, unit, system "http://unitsofmeasure.org", and UCUM code).
- Set "status": "final" and an appropriate "category".
- Set "subject": {{"reference": "Patient/{patient_id}"}}.
- Include "effectiveDateTime" using any date mentioned in the note (ISO 8601).

Return ONLY a JSON array of FHIR R4 Observation resources. No prose, no markdown fences."""
    text = _call(prompt, max_tokens=8192, model=model, think=True,
                 system="You are a precise FHIR R4 authoring assistant. Output valid JSON only.")
    data = _loads_json(text)
    if isinstance(data, dict):  # tolerate {"observations": [...]} or a single resource
        data = data.get("observations") or data.get("entry") or [data]
    return data


# --------------------------------------------------------------------------- #
# GA-02 · Clinical Timeline Narrative Generator
# --------------------------------------------------------------------------- #
def timeline_to_clinical_narrative(timeline: dict[str, Any], format_type: str = "referral_letter",
                                   model: str = DEFAULT_MODEL) -> str:
    """Turn a patient_timeline.json into a readable clinical narrative."""
    prompt = f"""You are writing a clinical handover note for an Indian endocrinologist.

Patient timeline data (JSON):
{json.dumps(timeline, indent=2)}

Format: {format_type}  (one of: referral_letter / handover_note / patient_summary)

Write in clinical English. Include:
- Disease history with timeline
- Key investigations and their trends
- Current medications
- Clinical impression
- Pending actions

Do not fabricate any clinical information not present in the timeline data."""
    return _call(prompt, max_tokens=4096, model=model, think=True,
                 system="You are an experienced clinician writing concise, accurate handover notes.")


# --------------------------------------------------------------------------- #
# GA-03 · FHIR Resource Validator & Explainer
# --------------------------------------------------------------------------- #
EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "field": {"type": "string"},
        "corrected_snippet": {"type": "string"},
    },
    "required": ["explanation", "field", "corrected_snippet"],
    "additionalProperties": False,
}


def explain_fhir_validation_error(operation_outcome: dict[str, Any], resource_json: dict[str, Any],
                                  model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Explain a FHIR validation OperationOutcome in plain English with a fix."""
    prompt = f"""A FHIR R4 server returned this validation error for a resource I submitted.

OperationOutcome: {json.dumps(operation_outcome)}
Resource submitted: {json.dumps(resource_json)}

Explain:
1. What is wrong, in plain English (no FHIR jargon).
2. Which field needs to change ("field").
3. Provide the corrected JSON snippet for that field only ("corrected_snippet", as a JSON string)."""
    text = _call(prompt, max_tokens=2048, model=model, json_schema=EXPLAIN_SCHEMA,
                 system="You are a patient FHIR R4 tutor. Be concrete and correct.")
    return _loads_json(text)


# --------------------------------------------------------------------------- #
# GA-04 · LOINC Code Recommendation Engine
# --------------------------------------------------------------------------- #
LOINC_SCHEMA = {
    "type": "object",
    "properties": {
        "loinc_code": {"type": "string"},
        "display_name": {"type": "string"},
        "specimen": {"type": "string"},
        "method": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "alternative_codes": {"type": "array", "items": {"type": "string"}},
        "why_this_code": {"type": "string"},
    },
    "required": ["loinc_code", "display_name", "specimen", "method",
                 "confidence", "alternative_codes", "why_this_code"],
    "additionalProperties": False,
}


def recommend_loinc_code(test_description: str, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Recommend the most specific LOINC code for a lab test description."""
    prompt = f"""Recommend the most specific LOINC code for this lab test.
Test: "{test_description}"

Return the LOINC code, display name, specimen, method, a confidence level,
alternative codes, and a short justification."""
    text = _call(prompt, max_tokens=1024, model=model, json_schema=LOINC_SCHEMA,
                 system="You are a clinical terminology expert specialising in LOINC.")
    return _loads_json(text)


# --------------------------------------------------------------------------- #
# GA-05 · FHIR Diff & Change Detection
# --------------------------------------------------------------------------- #
def clinical_diff(timeline_v1: dict[str, Any], timeline_v2: dict[str, Any],
                  model: str = DEFAULT_MODEL) -> str:
    """Summarise the clinical changes between two patient timelines."""
    prompt = f"""Compare these two patient clinical timelines and describe what changed.

Previous state:
{json.dumps(timeline_v1, indent=2)}

Current state:
{json.dumps(timeline_v2, indent=2)}

Summarise the changes in plain clinical language:
- New diagnoses added
- Medications changed or discontinued
- Lab values trending up or down
- New procedures or encounters

Only describe changes supported by the data."""
    return _call(prompt, max_tokens=3072, model=model, think=True,
                 system="You are a clinician summarising changes between two record snapshots.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_json_file(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM-powered FHIR helpers (PRD GA-01..05).")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Claude model id.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="GA-01: clinical note -> FHIR Observations")
    p_gen.add_argument("--note", required=True)
    p_gen.add_argument("--patient-id", default="rajesh-sharma")
    p_gen.add_argument("--out", default=None, help="Optional path to write the JSON array.")

    p_nar = sub.add_parser("narrative", help="GA-02: timeline -> clinical narrative")
    p_nar.add_argument("--timeline", default="output/patient_timeline.json")
    p_nar.add_argument("--format", default="referral_letter",
                       choices=["referral_letter", "handover_note", "patient_summary"])

    p_exp = sub.add_parser("explain", help="GA-03: explain a validation OperationOutcome")
    p_exp.add_argument("--outcome", required=True, help="Path to an OperationOutcome JSON.")
    p_exp.add_argument("--resource", required=True, help="Path to the submitted resource JSON.")

    p_loinc = sub.add_parser("loinc", help="GA-04: recommend a LOINC code")
    p_loinc.add_argument("--test", required=True, help="Lab test description.")

    p_diff = sub.add_parser("diff", help="GA-05: clinical diff between two timelines")
    p_diff.add_argument("--before", required=True)
    p_diff.add_argument("--after", required=True)

    args = parser.parse_args(argv)

    if args.command == "generate":
        resources = clinical_note_to_fhir(args.note, args.patient_id, model=args.model)
        out = json.dumps(resources, indent=2, ensure_ascii=False)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(out + "\n")
            print(f"Wrote {len(resources)} Observation(s) to {args.out}")
        print(out)
        print("\nNOTE: validate these against HAPI before trusting them "
              "(LLM output can be clinically wrong).", file=sys.stderr)

    elif args.command == "narrative":
        timeline = _load_json_file(args.timeline)
        print(timeline_to_clinical_narrative(timeline, args.format, model=args.model))

    elif args.command == "explain":
        outcome = _load_json_file(args.outcome)
        resource = _load_json_file(args.resource)
        result = explain_fhir_validation_error(outcome, resource, model=args.model)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "loinc":
        print(json.dumps(recommend_loinc_code(args.test, model=args.model), indent=2, ensure_ascii=False))

    elif args.command == "diff":
        before = _load_json_file(args.before)
        after = _load_json_file(args.after)
        print(clinical_diff(before, after, model=args.model))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
