#!/usr/bin/env python3
"""Validate and submit FHIR R4 resources to a HAPI FHIR server.

This script implements functional requirement F-02 of the PRD:

    * Loads every resource JSON file from ``resources/`` in dependency order
      (Patient -> Practitioner -> Encounter -> everything that references them).
    * Rewrites local ("logical") references such as ``Patient/rajesh-sharma`` to
      the server-assigned ids returned by the FHIR server after each create
      (F-02.4 -- "references break after HAPI assigns new IDs").
    * POSTs each resource to ``{base_url}/{ResourceType}`` and records the HTTP
      status code, the ``Location`` header and the server-assigned id (F-02.2).
    * Parses the ``OperationOutcome`` for any non-success response and documents
      the issues (F-02.3).
    * Writes ``output/validation_report.txt`` (F-02.5) and ``output/id_map.json``
      (consumed by ``timeline_builder.py``).

The script is offline-safe: ``--dry-run`` performs the load + reference rewrite
and writes a report without touching the network, which is handy for verifying
the linking logic. A real run only needs the ``requests`` library.

Usage::

    python validate_resources.py                      # POST to public HAPI sandbox
    python validate_resources.py --dry-run            # no network, just rewrite + report
    python validate_resources.py --base-url <url>     # use a different FHIR server
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import sys
import time
import uuid
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - requests is the only third-party dep
    requests = None  # handled gracefully in main()

DEFAULT_BASE_URL = "https://hapi.fhir.org/baseR4"
RESOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# Lower number == created earlier. Anything not listed defaults to 99 so it is
# created after the resources it might reference.
TYPE_ORDER = {
    "Patient": 0,
    "Practitioner": 1,
    "Questionnaire": 1,
    "Encounter": 2,
    "Condition": 3,
    "Observation": 3,
    "MedicationRequest": 3,
    "AllergyIntolerance": 3,
    "QuestionnaireResponse": 4,
    "Provenance": 5,
}

# --------------------------------------------------------------------------- #
# Per-run uniqueness helpers (shared with submit_bundle.py).
#
# The public HAPI sandbox rejects creating a resource whose content is
# byte-identical to one already stored (error HAPI-2840). On a re-run that makes
# the one-by-one submission fail: the Patient/Practitioner/Questionnaire already
# exist, their POSTs 412, no server id is captured, and every dependent resource
# then 400s with "reference not found". Stamping each resource with a unique
# per-run meta.tag + identifier keeps every submission distinct, so a re-run
# creates a fresh set and returns 201 across the board.
# --------------------------------------------------------------------------- #
RUN_TAG_SYSTEM = "urn:fhir-modelling:submission-run"
# Provenance has no `identifier` element in R4; it varies naturally because its
# targets resolve to the freshly-created resource ids.
NO_IDENTIFIER_TYPES = {"Provenance"}
# QuestionnaireResponse.identifier is a single Identifier (0..1), not a list.
SINGLE_IDENTIFIER_TYPES = {"QuestionnaireResponse"}

# Matches HAPI-2840 ("...duplicating existing resource: Patient/12345").
_DUPLICATE_RE = re.compile(r"existing resource:\s*([A-Za-z]+)/(\S+)")


def add_unique_identifier(resource: dict[str, Any], value: str) -> None:
    """Attach a unique business identifier so the content is not a duplicate."""
    rtype = resource.get("resourceType", "")
    if rtype in NO_IDENTIFIER_TYPES:
        return
    ident = {"system": RUN_TAG_SYSTEM, "value": value}
    if rtype in SINGLE_IDENTIFIER_TYPES:
        resource.setdefault("identifier", ident)
    else:
        resource.setdefault("identifier", []).append(ident)


def stamp_unique(resource: dict[str, Any], run_tag: str, local_id: str | None) -> None:
    """Stamp a resource with a per-run meta.tag + unique identifier (in place)."""
    meta_block = resource.setdefault("meta", {})
    meta_block.setdefault("tag", []).append({"system": RUN_TAG_SYSTEM, "code": run_tag})
    add_unique_identifier(resource, f"{run_tag}-{local_id}")


def existing_ref_from_issues(issues: list[str]) -> str | None:
    """Extract a 'Type/id' reference from a HAPI-2840 duplicate-resource issue."""
    for issue in issues:
        match = _DUPLICATE_RE.search(issue)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


# Stable business-identifier namespace stamped on the resource files (see the
# resource JSON). Lets every resource be upserted deterministically.
RESOURCE_KEY_SYSTEM = "urn:fhir-modelling:resource-key"


def _identifiers(resource: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise a resource's identifier element (list, single object, or absent)."""
    idf = resource.get("identifier")
    if isinstance(idf, dict):
        return [idf]
    if isinstance(idf, list):
        return idf
    return []


def upsert_search(resource: dict[str, Any]) -> str | None:
    """Build the conditional-update query for a resource.

    Returns ``identifier=<system>|<value>`` from the resource's first business
    identifier (ABHA for Patient, NMC for Practitioner, the resource-key for the
    rest), or ``_tag=<system>|<code>`` for Provenance (which has no identifier
    element in R4). A ``PUT {base}/{Type}?<this>`` then creates the resource if no
    match exists and updates it in place if exactly one does -- i.e. an idempotent
    upsert keyed on a stable identity, so re-runs never create duplicates.
    """
    for ident in _identifiers(resource):
        system, value = ident.get("system"), ident.get("value")
        if system and value:
            return f"identifier={system}|{value}"
    for tag in (resource.get("meta") or {}).get("tag", []) or []:
        system, code = tag.get("system"), tag.get("code")
        if system and code:
            return f"_tag={system}|{code}"
    return None


def load_resources(resources_dir: str) -> list[dict[str, Any]]:
    """Read every ``*.json`` file and return them sorted in dependency order.

    Each returned item is a dict with ``filename``, ``local_id`` and the parsed
    ``resource`` body. Sorting is stable, so files of equal priority keep their
    alphabetical order.
    """
    entries: list[dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(resources_dir, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            resource = json.load(fh)
        rtype = resource.get("resourceType", "")
        entries.append(
            {
                "filename": os.path.basename(path),
                "resource_type": rtype,
                "local_id": resource.get("id"),
                "resource": resource,
            }
        )
    entries.sort(key=lambda e: TYPE_ORDER.get(e["resource_type"], 99))
    return entries


def rewrite_references(node: Any, id_map: dict[str, str]) -> None:
    """Recursively rewrite ``reference`` strings in place using ``id_map``.

    ``id_map`` maps a local reference (e.g. ``"Patient/rajesh-sharma"``) to the
    server reference (e.g. ``"Patient/12345"``). Any FHIR element with a
    ``reference`` key whose value is present in the map is updated.
    """
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str) and ref in id_map:
            node["reference"] = id_map[ref]
        for value in node.values():
            rewrite_references(value, id_map)
    elif isinstance(node, list):
        for item in node:
            rewrite_references(item, id_map)


def parse_operation_outcome(body: dict[str, Any]) -> list[str]:
    """Return a list of human-readable issue strings from an OperationOutcome."""
    issues: list[str] = []
    if not isinstance(body, dict) or body.get("resourceType") != "OperationOutcome":
        return issues
    for issue in body.get("issue", []):
        severity = issue.get("severity", "?")
        code = issue.get("code", "?")
        details = ""
        if isinstance(issue.get("details"), dict):
            details = issue["details"].get("text", "")
        diagnostics = issue.get("diagnostics", "")
        location = ", ".join(issue.get("expression", []) or issue.get("location", []) or [])
        text = f"[{severity}/{code}] {details or diagnostics}".strip()
        if location:
            text += f" (at: {location})"
        issues.append(text)
    return issues


def server_id_from_response(response: "requests.Response", body: dict[str, Any]) -> str | None:
    """Extract the server-assigned logical id from a create response.

    Prefers the parsed body's ``id``; falls back to parsing the ``Location``
    header which looks like ``.../Patient/12345/_history/1``.
    """
    if isinstance(body, dict) and body.get("id"):
        return str(body["id"])
    location = response.headers.get("Location") or response.headers.get("Content-Location")
    if location:
        parts = location.rstrip("/").split("/")
        if "_history" in parts:
            parts = parts[: parts.index("_history")]
        if len(parts) >= 1:
            return parts[-1]
    return None


def send_resource(
    base_url: str,
    resource: dict[str, Any],
    timeout: int,
    method: str = "POST",
    search: str | None = None,
) -> tuple[int | None, dict[str, Any], "requests.Response | None", str | None]:
    """Submit a single resource. Returns (status_code, body, response, error).

    ``method="POST"`` creates a new resource (``{base}/{Type}``). ``method="PUT"``
    with a ``search`` string performs a conditional update / upsert
    (``{base}/{Type}?{search}``): create if no match, update in place if one match.
    """
    rtype = resource["resourceType"]
    base = base_url.rstrip("/")
    headers = {
        "Content-Type": "application/fhir+json",
        "Accept": "application/fhir+json",
    }
    try:
        if method == "PUT" and search:
            response = requests.put(f"{base}/{rtype}?{search}", json=resource, headers=headers, timeout=timeout)
        else:
            response = requests.post(f"{base}/{rtype}", json=resource, headers=headers, timeout=timeout)
    except Exception as exc:  # network error, DNS failure, timeout, etc.
        return None, {}, None, str(exc)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body, response, None


def write_report(report_path: str, base_url: str, dry_run: bool, results: list[dict[str, Any]]) -> None:
    """Write the human-readable validation report (F-02.5)."""
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    success = sum(1 for r in results if r.get("status") in (200, 201))
    lines = [
        "FHIR Resource Validation Report",
        "=" * 70,
        f"Generated : {now}",
        f"Server    : {base_url}",
        f"Mode      : {'DRY RUN (no network)' if dry_run else 'LIVE POST'}",
        f"Resources : {len(results)}  |  Accepted (2xx): {success}",
        "=" * 70,
        "",
    ]
    for idx, r in enumerate(results, 1):
        lines.append(f"{idx:>2}. {r['filename']}  ({r['resource_type']})")
        lines.append(f"    Local id        : {r.get('local_id')}")
        if dry_run:
            lines.append("    HTTP status     : (dry run - not submitted)")
        elif r.get("error"):
            lines.append(f"    HTTP status     : ERROR - {r['error']}")
        else:
            lines.append(f"    HTTP status     : {r.get('status')}")
            lines.append(f"    Location header : {r.get('location') or '-'}")
            lines.append(f"    Server id       : {r.get('server_ref') or '-'}")
        if r.get("rewritten_refs"):
            for src, dst in r["rewritten_refs"].items():
                lines.append(f"    Reference rewritten: {src} -> {dst}")
        if r.get("issues"):
            lines.append("    OperationOutcome issues:")
            for issue in r["issues"]:
                lines.append(f"      - {issue}")
        elif not dry_run and r.get("status") in (200, 201):
            lines.append("    OperationOutcome issues: none")
        lines.append("")

    lines.append("=" * 70)
    if dry_run:
        lines.append("Dry run complete. Re-run without --dry-run to submit to the server.")
    elif success == len(results):
        lines.append("All resources accepted by the FHIR server (HTTP 2xx).")
    else:
        lines.append(
            f"{len(results) - success} resource(s) were not accepted. "
            "Review the issues above, fix the JSON, and re-run."
        )
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate/submit FHIR resources to a HAPI server.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="FHIR server base URL.")
    parser.add_argument("--resources-dir", default=RESOURCES_DIR, help="Directory of resource JSON files.")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="Where to write the report and id map.")
    parser.add_argument("--dry-run", action="store_true", help="Do not POST; just rewrite refs and report.")
    parser.add_argument("--timeout", type=int, default=30, help="Per-request timeout in seconds.")
    parser.add_argument("--delay", type=float, default=0.3, help="Delay between POSTs (seconds).")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Throwaway-demo mode: POST a brand-new, per-run-unique copy of every "
        "resource instead of upserting. Leaves duplicate identities on the server "
        "(fine for a one-off sandbox demo, wrong for real data). Default is an "
        "idempotent upsert (conditional update) so one ABHA maps to one Patient.",
    )
    args = parser.parse_args(argv)

    os.makedirs(args.output_dir, exist_ok=True)

    if not args.dry_run and requests is None:
        print("ERROR: the 'requests' library is required for live submission.", file=sys.stderr)
        print("Install it with:  pip install -r requirements.txt", file=sys.stderr)
        print("Or run with --dry-run to test the linking logic offline.", file=sys.stderr)
        return 1

    entries = load_resources(args.resources_dir)
    if not entries:
        print(f"No resource files found in {args.resources_dir}", file=sys.stderr)
        return 1

    id_map: dict[str, str] = {}  # "Patient/rajesh-sharma" -> "Patient/12345"
    results: list[dict[str, Any]] = []
    patient_server_id: str | None = None
    abha_id: str | None = None

    # Default is an idempotent upsert (conditional update keyed on a stable
    # identifier) so re-runs update the same resources in place. --fresh instead
    # POSTs a unique per-run copy of everything (throwaway demo, leaves duplicates).
    run_tag = f"run-{uuid.uuid4().hex[:8]}" if args.fresh else None

    submit_mode = "FRESH (per-run create)" if args.fresh else "UPSERT (conditional update)"
    print(f"Loaded {len(entries)} resources. Mode: {'DRY RUN' if args.dry_run else 'LIVE'} / {submit_mode}")
    if run_tag:
        print(f"Run tag: {run_tag}  (each resource stamped unique)")
    print()

    for entry in entries:
        resource = entry["resource"]
        rtype = entry["resource_type"]
        local_id = entry["local_id"]

        # Snapshot which references this resource carries before rewriting, so we
        # can report exactly what changed.
        before = _collect_references(resource)
        rewrite_references(resource, id_map)
        after = _collect_references(resource)
        rewritten = {b: a for b, a in zip(before, after) if b != a}

        # The server owns the logical id (assigned on create, preserved on a
        # conditional update), so drop any local id from the body.
        resource.pop("id", None)

        if run_tag:
            # --fresh: stamp a per-run tag + unique identifier so every run creates
            # a brand-new, distinct resource (no HAPI-2840 collision).
            stamp_unique(resource, run_tag, local_id)
            method, search = "POST", None
        else:
            # Default upsert: conditional update keyed on the stable identifier.
            method, search = "PUT", upsert_search(resource)

        result: dict[str, Any] = {
            "filename": entry["filename"],
            "resource_type": rtype,
            "local_id": local_id,
            "rewritten_refs": rewritten,
        }

        if rtype == "Patient":
            for ident in resource.get("identifier", []):
                if ident.get("system") == "https://abdm.gov.in/ABHA":
                    abha_id = ident.get("value")

        if args.dry_run:
            how = f"{method} {('?' + search) if search else '(new)'}"
            print(f"  [dry-run] {entry['filename']} ({rtype}) {how}; refs rewritten: {len(rewritten)}")
            results.append(result)
            continue

        status, body, response, error = send_resource(
            args.base_url, resource, args.timeout, method=method, search=search
        )
        result["status"] = status
        result["error"] = error

        if error is not None:
            print(f"  [ERROR ] {entry['filename']}: {error}")
            results.append(result)
            continue

        result["location"] = response.headers.get("Location") or response.headers.get("Content-Location")
        result["issues"] = parse_operation_outcome(body)

        if status in (200, 201):
            server_id = server_id_from_response(response, body)
            if server_id and local_id:
                server_ref = f"{rtype}/{server_id}"
                id_map[f"{rtype}/{local_id}"] = server_ref
                result["server_ref"] = server_ref
                if rtype == "Patient":
                    patient_server_id = server_id
            print(f"  [{status:>3}  ] {entry['filename']} -> {result.get('server_ref') or '?'}")
        else:
            # Defensive fallback: if the server rejected this as a duplicate
            # (HAPI-2840), it tells us the id of the existing resource. Reuse that
            # id so downstream references still rewrite.
            existing_ref = existing_ref_from_issues(result["issues"])
            if existing_ref and local_id:
                id_map[f"{rtype}/{local_id}"] = existing_ref
                result["server_ref"] = existing_ref
                if rtype == "Patient":
                    patient_server_id = existing_ref.split("/")[-1]
                print(f"  [{status:>3}  ] {entry['filename']}: duplicate, reusing {existing_ref}")
            else:
                print(f"  [{status:>3}  ] {entry['filename']}: {len(result['issues'])} issue(s)")

        results.append(result)
        if args.delay:
            time.sleep(args.delay)

    report_path = os.path.join(args.output_dir, "validation_report.txt")
    write_report(report_path, args.base_url, args.dry_run, results)
    print(f"\nReport written to {report_path}")

    if not args.dry_run:
        id_map_path = os.path.join(args.output_dir, "id_map.json")
        with open(id_map_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "base_url": args.base_url,
                    "abha_id": abha_id,
                    "patient_server_id": patient_server_id,
                    "references": id_map,
                },
                fh,
                indent=2,
            )
        print(f"Id map written to {id_map_path}")
        if patient_server_id:
            print(f"\nNext step:\n  python timeline_builder.py {patient_server_id}")

    return 0


def _collect_references(node: Any, acc: list[str] | None = None) -> list[str]:
    """Collect all ``reference`` string values in document order (for reporting)."""
    if acc is None:
        acc = []
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str):
            acc.append(ref)
        for value in node.values():
            _collect_references(value, acc)
    elif isinstance(node, list):
        for item in node:
            _collect_references(item, acc)
    return acc


if __name__ == "__main__":
    raise SystemExit(main())
