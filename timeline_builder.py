#!/usr/bin/env python3
"""Reconstruct a chronological clinical timeline from FHIR resources.

This implements functional requirement F-03 of the PRD. It fetches a patient's
resources from a HAPI FHIR server using FHIR search, merges them into a single
chronological list grouped by encounter, prints a readable timeline to the
console, and exports a structured ``patient_timeline.json``.

The script depends only on the Python standard library plus ``requests``.

Usage::

    # By HAPI-assigned numeric id:
    python timeline_builder.py 12345

    # By ABHA id (uses Patient?identifier= search):
    python timeline_builder.py 1234-5678-9012-3456

    # Re-use the patient id captured by validate_resources.py:
    python timeline_builder.py auto

    # Offline: build the timeline straight from the local resource files
    # (handy for testing or when the sandbox is unavailable):
    python timeline_builder.py --offline-dir resources

All resource access is defensive: missing fields never raise, they are simply
omitted from the output (NFR "handle missing fields gracefully").
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sys
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

DEFAULT_BASE_URL = "https://hapi.fhir.org/baseR4"
HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
ID_MAP_PATH = os.path.join(OUTPUT_DIR, "id_map.json")

CLASS_LABELS = {"AMB": "Ambulatory / OPD", "IMP": "Inpatient", "EMER": "Emergency"}


# --------------------------------------------------------------------------- #
# Small, defensive field accessors
# --------------------------------------------------------------------------- #
def _first(seq: Any) -> Any:
    """Return the first element of a list, or None."""
    if isinstance(seq, list) and seq:
        return seq[0]
    return None


def coding_display(codeable: dict[str, Any]) -> str:
    """Best-effort display text for a CodeableConcept."""
    if not isinstance(codeable, dict):
        return ""
    if codeable.get("text"):
        return codeable["text"]
    coding = _first(codeable.get("coding")) or {}
    return coding.get("display") or coding.get("code") or ""


def coding_code(codeable: dict[str, Any]) -> str:
    """Return the primary code string of a CodeableConcept."""
    coding = _first((codeable or {}).get("coding")) or {}
    return coding.get("code", "")


def parse_fhir_datetime(value: str | None) -> _dt.datetime | None:
    """Parse a FHIR ``date``/``dateTime`` to a naive (wall-clock) datetime.

    Timezone info is dropped so that date-only and dateTime values (and values
    with offsets) can be compared and sorted together. All scenario data is in
    IST, so wall-clock ordering matches true chronology.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    candidates = [text]
    if len(text) == 10:  # date only
        candidates = [text + "T00:00:00"]
    for cand in candidates:
        try:
            dt = _dt.datetime.fromisoformat(cand.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None)
        except ValueError:
            continue
    # Last resort: take the leading date portion.
    try:
        return _dt.datetime.fromisoformat(text[:10] + "T00:00:00")
    except ValueError:
        return None


def resource_datetime(resource: dict[str, Any]) -> _dt.datetime | None:
    """Pick the most relevant timestamp from any clinical resource."""
    for field in ("effectiveDateTime", "onsetDateTime", "authoredOn", "recordedDate"):
        if resource.get(field):
            return parse_fhir_datetime(resource[field])
    period = resource.get("period")
    if isinstance(period, dict) and period.get("start"):
        return parse_fhir_datetime(period["start"])
    return None


def ref_id(reference: str | None) -> str | None:
    """Return the logical id from a reference string like ``Encounter/99010``."""
    if not reference or not isinstance(reference, str):
        return None
    return reference.rstrip("/").split("/")[-1]


def quantity_str(q: dict[str, Any]) -> str:
    """Render a Quantity as ``value unit`` (e.g. ``185 mg/dL``)."""
    if not isinstance(q, dict):
        return ""
    value = q.get("value")
    unit = q.get("unit") or q.get("code") or ""
    if value is None:
        return ""
    # Trim trailing .0 for whole numbers.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value} {unit}".strip()


def dosage_summary(med: dict[str, Any]) -> str:
    """Short dosage string for a MedicationRequest."""
    di = _first(med.get("dosageInstruction")) or {}
    if di.get("text"):
        return di["text"]
    repeat = (di.get("timing") or {}).get("repeat") or {}
    freq = repeat.get("frequency")
    period = repeat.get("period")
    unit = repeat.get("periodUnit")
    if freq and period and unit:
        return f"{freq} time(s) per {period}{unit}"
    return ""


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def _bundle_resources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract resource bodies from a search-set Bundle's entries."""
    out = []
    for entry in bundle.get("entry", []) or []:
        res = entry.get("resource")
        if isinstance(res, dict):
            out.append(res)
    return out


def _next_link(bundle: dict[str, Any]) -> str | None:
    for link in bundle.get("link", []) or []:
        if link.get("relation") == "next":
            return link.get("url")
    return None


def fetch_search(base_url: str, path: str, timeout: int, max_pages: int = 20) -> list[dict[str, Any]]:
    """Run a FHIR search and return all matching resources (following pages)."""
    headers = {"Accept": "application/fhir+json"}
    url = f"{base_url.rstrip('/')}/{path}"
    resources: list[dict[str, Any]] = []
    pages = 0
    while url and pages < max_pages:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        bundle = resp.json()
        resources.extend(_bundle_resources(bundle))
        url = _next_link(bundle)
        pages += 1
    return resources


def fetch_from_server(base_url: str, patient_arg: str, timeout: int) -> dict[str, list[dict[str, Any]]]:
    """Resolve the patient and fetch all related resources via FHIR search."""
    if patient_arg.isdigit():
        patient_id = patient_arg
        resp = requests.get(
            f"{base_url.rstrip('/')}/Patient/{patient_id}",
            headers={"Accept": "application/fhir+json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        patients = [resp.json()]
    else:
        # Treat as an identifier (e.g. ABHA id).
        patients = fetch_search(base_url, f"Patient?identifier={patient_arg}", timeout)
        if not patients:
            raise SystemExit(f"No Patient found with identifier '{patient_arg}' on {base_url}")
        patient_id = patients[0].get("id")
        if not patient_id:
            raise SystemExit("Resolved a Patient but it has no server id.")

    print(f"Resolved patient server id: {patient_id}")
    return {
        "Patient": patients,
        "Encounter": fetch_search(base_url, f"Encounter?patient={patient_id}", timeout),
        "Observation": fetch_search(base_url, f"Observation?patient={patient_id}&_sort=date", timeout),
        "Condition": fetch_search(base_url, f"Condition?patient={patient_id}", timeout),
        "MedicationRequest": fetch_search(base_url, f"MedicationRequest?patient={patient_id}", timeout),
    }


def load_from_dir(directory: str) -> dict[str, list[dict[str, Any]]]:
    """Build the same resource buckets from local JSON files (offline mode)."""
    buckets: dict[str, list[dict[str, Any]]] = {
        "Patient": [], "Encounter": [], "Observation": [],
        "Condition": [], "MedicationRequest": [],
    }
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            res = json.load(fh)
        rtype = res.get("resourceType")
        if rtype in buckets:
            buckets[rtype].append(res)
    return buckets


# --------------------------------------------------------------------------- #
# Timeline assembly
# --------------------------------------------------------------------------- #
def build_timeline(buckets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Merge resources into a chronological, encounter-grouped event list."""
    patient = _first(buckets.get("Patient")) or {}
    patient_info = {
        "name": coding_name(patient),
        "abha_id": abha_of(patient),
        "server_id": patient.get("id"),
    }

    # Index encounters by their id and create one event per encounter.
    events_by_enc: dict[str, dict[str, Any]] = {}
    encounter_order: list[str] = []
    for enc in buckets.get("Encounter", []):
        enc_id = enc.get("id")
        if enc_id is None:
            continue
        cls = (enc.get("class") or {}).get("code", "")
        start = resource_datetime(enc)
        end = parse_fhir_datetime((enc.get("period") or {}).get("end"))
        events_by_enc[enc_id] = {
            "date": _date_str(start),
            "_sortkey": start or _dt.datetime.max,
            "type": "Encounter",
            "encounter_class": cls,
            "title": coding_display(_first(enc.get("type")) or {}) or CLASS_LABELS.get(cls, "Encounter"),
            "practitioner": practitioner_of(enc),
            "period_end": _date_str(end),
            "conditions": [],
            "labs": [],
            "vitals": [],
            "medications": [],
        }
        encounter_order.append(enc_id)

    standalone: list[dict[str, Any]] = []

    def target_event(resource: dict[str, Any]) -> dict[str, Any] | None:
        enc_ref = ref_id((resource.get("encounter") or {}).get("reference"))
        if enc_ref and enc_ref in events_by_enc:
            return events_by_enc[enc_ref]
        return None

    # Conditions
    for cond in buckets.get("Condition", []):
        item = {"display": coding_display(cond.get("code") or {}), "code": coding_code(cond.get("code") or {})}
        ev = target_event(cond)
        if ev:
            ev["conditions"].append(item)
        else:
            standalone.append(_standalone(cond, "Condition", item["display"]))

    # Observations -> split into labs vs vitals by category
    for obs in buckets.get("Observation", []):
        item = {
            "display": coding_display(obs.get("code") or {}),
            "code": coding_code(obs.get("code") or {}),
            "value": quantity_str(obs.get("valueQuantity") or {}),
        }
        ev = target_event(obs)
        bucket = "vitals" if is_vital_sign(obs) else "labs"
        if ev:
            ev[bucket].append(item)
        else:
            standalone.append(_standalone(obs, "Observation", f"{item['display']} {item['value']}".strip()))

    # MedicationRequests
    for med in buckets.get("MedicationRequest", []):
        item = {
            "display": coding_display(med.get("medicationCodeableConcept") or {}),
            "dosage": dosage_summary(med),
        }
        ev = target_event(med)
        if ev:
            ev["medications"].append(item)
        else:
            standalone.append(_standalone(med, "MedicationRequest", item["display"]))

    # Assemble: encounter events + synthesized discharge events + standalones.
    events: list[dict[str, Any]] = [events_by_enc[e] for e in encounter_order]

    for enc_id in encounter_order:
        ev = events_by_enc[enc_id]
        if ev["encounter_class"] == "IMP" and ev["period_end"]:
            events.append({
                "date": ev["period_end"],
                "_sortkey": parse_fhir_datetime(ev["period_end"]) or _dt.datetime.max,
                "type": "Discharge",
                "title": "Discharge",
                "encounter_title": ev["title"],
            })

    events.extend(standalone)
    events.sort(key=lambda e: e["_sortkey"])
    for e in events:
        e.pop("_sortkey", None)

    return {
        "patient": patient_info,
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_count": len(events),
        "events": events,
    }


def _standalone(resource: dict[str, Any], rtype: str, label: str) -> dict[str, Any]:
    dt = resource_datetime(resource)
    return {
        "date": _date_str(dt),
        "_sortkey": dt or _dt.datetime.max,
        "type": rtype,
        "title": label,
    }


def is_vital_sign(obs: dict[str, Any]) -> bool:
    for cat in obs.get("category", []) or []:
        for coding in (cat.get("coding") or []):
            if coding.get("code") == "vital-signs":
                return True
    return False


def coding_name(patient: dict[str, Any]) -> str:
    name = _first(patient.get("name")) or {}
    if name.get("text"):
        return name["text"]
    given = " ".join(name.get("given", []) or [])
    return f"{given} {name.get('family', '')}".strip() or "Unknown patient"


def abha_of(patient: dict[str, Any]) -> str | None:
    for ident in patient.get("identifier", []) or []:
        if ident.get("system") == "https://abdm.gov.in/ABHA":
            return ident.get("value")
    return None


def practitioner_of(enc: dict[str, Any]) -> str | None:
    for part in enc.get("participant", []) or []:
        individual = part.get("individual") or {}
        if individual.get("display"):
            return individual["display"]
    return None


def _date_str(dt: _dt.datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d") if dt else None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_console(timeline: dict[str, Any]) -> str:
    """Build the formatted console timeline string (PRD F-03.4 layout)."""
    p = timeline["patient"]
    lines = [
        "=" * 72,
        f"  CLINICAL TIMELINE  -  {p.get('name') or 'Unknown'}",
        f"  ABHA: {p.get('abha_id') or 'n/a'}   |   Server id: {p.get('server_id') or 'n/a'}",
        f"  Generated: {timeline['generated']}   |   Events: {timeline['event_count']}",
        "=" * 72,
        "",
    ]
    if not timeline["events"]:
        lines.append("  (no events found for this patient)")
        return "\n".join(lines)

    for ev in timeline["events"]:
        date = ev.get("date") or "????-??-??"
        if ev["type"] == "Encounter":
            cls = ev.get("encounter_class", "")
            who = f" ({ev['practitioner']})" if ev.get("practitioner") else ""
            cls_lbl = f" [{CLASS_LABELS.get(cls, cls)}]" if cls else ""
            lines.append(f"-- {date}  {ev.get('title') or 'Encounter'}{cls_lbl}{who}")
            if ev["conditions"]:
                joined = ", ".join(
                    f"{c['display']} ({c['code']})" if c.get("code") else c["display"]
                    for c in ev["conditions"]
                )
                lines.append(f"     Conditions : {joined}")
            if ev["labs"]:
                joined = ", ".join(f"{l['display']} {l['value']}".strip() for l in ev["labs"])
                lines.append(f"     Labs       : {joined}")
            if ev["vitals"]:
                joined = ", ".join(f"{v['display']} {v['value']}".strip() for v in ev["vitals"])
                lines.append(f"     Vitals     : {joined}")
            if ev["medications"]:
                joined = ", ".join(
                    f"{m['display']} [{m['dosage']}]" if m.get("dosage") else m["display"]
                    for m in ev["medications"]
                )
                lines.append(f"     Medications: {joined}")
        elif ev["type"] == "Discharge":
            lines.append(f"-- {date}  Discharge  ({ev.get('encounter_title', '')})")
        else:
            lines.append(f"-- {date}  {ev['type']}: {ev.get('title', '')}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _resolve_patient_arg(arg: str | None) -> tuple[str, str]:
    """Resolve the patient argument, honouring the 'auto' keyword via id_map.json.

    Returns (patient_arg, base_url_hint). base_url_hint is '' unless id_map
    supplies one.
    """
    base_hint = ""
    if arg is None or arg == "auto":
        if not os.path.exists(ID_MAP_PATH):
            raise SystemExit(
                "No patient id given and output/id_map.json not found.\n"
                "Run validate_resources.py first, or pass an ABHA / server id explicitly."
            )
        with open(ID_MAP_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        base_hint = data.get("base_url", "")
        resolved = data.get("patient_server_id") or data.get("abha_id")
        if not resolved:
            raise SystemExit("id_map.json has no patient id to use.")
        print(f"Using patient id from id_map.json: {resolved}")
        return str(resolved), base_hint
    return arg, base_hint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a clinical timeline from FHIR resources.")
    parser.add_argument("patient", nargs="?", help="ABHA id, HAPI server id, or 'auto' (use id_map.json).")
    parser.add_argument("--base-url", default=None, help="FHIR server base URL.")
    parser.add_argument("--offline-dir", default=None, help="Build from local JSON files instead of the server.")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="Where to write patient_timeline.json.")
    parser.add_argument("--timeout", type=int, default=30, help="Per-request timeout in seconds.")
    args = parser.parse_args(argv)

    os.makedirs(args.output_dir, exist_ok=True)

    if args.offline_dir:
        print(f"Offline mode: reading resources from {args.offline_dir}")
        buckets = load_from_dir(args.offline_dir)
    else:
        if requests is None:
            print("ERROR: the 'requests' library is required (or use --offline-dir).", file=sys.stderr)
            return 1
        patient_arg, base_hint = _resolve_patient_arg(args.patient)
        base_url = args.base_url or base_hint or DEFAULT_BASE_URL
        try:
            buckets = fetch_from_server(base_url, patient_arg, args.timeout)
        except Exception as exc:
            print(f"ERROR fetching from {base_url}: {exc}", file=sys.stderr)
            return 1

    timeline = build_timeline(buckets)
    print()
    print(render_console(timeline))

    out_path = os.path.join(args.output_dir, "patient_timeline.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(timeline, fh, indent=2, ensure_ascii=False)
    print(f"Timeline exported to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
