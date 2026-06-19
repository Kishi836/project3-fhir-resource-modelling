#!/usr/bin/env python3
"""Submit all FHIR resources to a HAPI server as a single transaction Bundle.

This implements additional feature AF-02 of the PRD. Instead of POSTing each
resource one-by-one (see ``validate_resources.py``), it assembles a FHIR
``transaction`` Bundle and submits everything in **one** atomic API call.

How the linking works
---------------------
Inside a transaction Bundle, resources reference each other by a temporary
``urn:uuid:...`` placed in each entry's ``fullUrl``. The server resolves those
placeholders to real ids atomically when it processes the bundle, so we never
need to know the server-assigned ids in advance. This script therefore:

    1. Loads every resource file and assigns each one a stable ``urn:uuid``.
    2. Rewrites every ``reference`` (e.g. ``Patient/rajesh-sharma``) to the
       corresponding ``urn:uuid``.
    3. Builds the transaction Bundle (one ``POST`` entry per resource).
    4. POSTs the Bundle and parses ``Bundle.entry[].response`` to extract every
       server-assigned id (F: "Handle the Bundle.entry.response array").

Usage::

    python submit_bundle.py                 # POST transaction Bundle to HAPI
    python submit_bundle.py --dry-run       # build + save the Bundle, no network
    python submit_bundle.py --base-url <url>
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import uuid
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# Reuse the loader, reference walker and per-run uniqueness helpers already
# written for the one-by-one path (kept there so the import direction stays
# one-way: submit_bundle -> validate_resources).
from validate_resources import (
    DEFAULT_BASE_URL,
    OUTPUT_DIR,
    RESOURCES_DIR,
    RUN_TAG_SYSTEM,
    add_unique_identifier,
    load_resources,
    rewrite_references,
    upsert_search,
)


def build_transaction_bundle(
    resources_dir: str, run_tag: str | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return a transaction Bundle and the {local_ref -> urn:uuid} map used.

    Default (``run_tag=None``) is an **idempotent upsert**: every entry is a
    conditional update (``PUT {Type}?identifier=…`` or ``?_tag=…`` for Provenance)
    keyed on the resource's stable business identifier, so re-submitting the Bundle
    updates the same resources in place instead of creating duplicates -- one ABHA
    maps to one Patient, exactly as a real integration must behave.

    Pass a ``run_tag`` (``--fresh``) for a throwaway demo: every entry becomes a
    plain ``POST`` stamped with a unique per-run ``meta.tag`` + ``identifier`` so a
    re-run always creates a brand-new, distinct set (HTTP 201) and never collides
    with content already on the shared sandbox (error HAPI-2840).
    """
    entries_meta = load_resources(resources_dir)

    # Assign a urn:uuid to every resource keyed by "ResourceType/local_id".
    uuid_map: dict[str, str] = {}
    for meta in entries_meta:
        if meta["local_id"]:
            key = f"{meta['resource_type']}/{meta['local_id']}"
            uuid_map[key] = f"urn:uuid:{uuid.uuid4()}"

    bundle_entries: list[dict[str, Any]] = []
    for meta in entries_meta:
        resource = meta["resource"]
        rtype = meta["resource_type"]
        rewrite_references(resource, uuid_map)  # point refs at urn:uuid placeholders
        resource.pop("id", None)  # server owns the logical id

        if run_tag:
            # --fresh: stamp unique tag + identifier and create a brand-new resource.
            meta_block = resource.setdefault("meta", {})
            meta_block.setdefault("tag", []).append({"system": RUN_TAG_SYSTEM, "code": run_tag})
            add_unique_identifier(resource, f"{run_tag}-{meta['local_id']}")
            request: dict[str, Any] = {"method": "POST", "url": rtype}
        else:
            # Default: conditional update (upsert) on the stable identifier.
            search = upsert_search(resource)
            if search:
                request = {"method": "PUT", "url": f"{rtype}?{search}"}
            else:  # no stable key -> fall back to plain create
                request = {"method": "POST", "url": rtype}

        bundle_entries.append(
            {
                "fullUrl": uuid_map.get(f"{rtype}/{meta['local_id']}"),
                "resource": resource,
                "request": request,
            }
        )

    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": bundle_entries,
    }
    return bundle, uuid_map


def parse_bundle_response(response_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract per-entry status + assigned location from a transaction-response."""
    rows: list[dict[str, Any]] = []
    for entry in response_bundle.get("entry", []) or []:
        resp = entry.get("response", {}) or {}
        location = resp.get("location", "")
        server_ref = ""
        if location:
            parts = location.split("/_history")[0].rstrip("/").split("/")
            if len(parts) >= 2:
                server_ref = f"{parts[-2]}/{parts[-1]}"
        rows.append({"status": resp.get("status", "?"), "location": location, "server_ref": server_ref})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit resources as one FHIR transaction Bundle.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--resources-dir", default=RESOURCES_DIR)
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Build the Bundle but do not POST.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Throwaway-demo mode: POST a brand-new, per-run-unique copy of every "
        "resource instead of upserting. Default is an idempotent upsert (conditional "
        "update) so re-submitting updates the same resources -- one ABHA, one Patient.",
    )
    parser.add_argument("--tag", default=None, help="Custom run tag for --fresh (default: auto-generated).")
    args = parser.parse_args(argv)

    os.makedirs(args.output_dir, exist_ok=True)

    run_tag = (args.tag or f"run-{uuid.uuid4().hex[:8]}") if args.fresh else None
    bundle, uuid_map = build_transaction_bundle(args.resources_dir, run_tag=run_tag)
    n = len(bundle["entry"])
    mode = f"FRESH (run tag {run_tag})" if run_tag else "UPSERT (conditional update)"
    print(f"Built transaction Bundle with {n} entries.  Mode: {mode}")

    bundle_path = os.path.join(args.output_dir, "transaction_bundle.json")
    with open(bundle_path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
    print(f"Bundle written to {bundle_path}")

    if args.dry_run:
        print("Dry run: not submitting. Re-run without --dry-run to POST.")
        return 0

    if requests is None:
        print("ERROR: 'requests' is required for submission (or use --dry-run).", file=sys.stderr)
        return 1

    headers = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
    try:
        resp = requests.post(args.base_url.rstrip("/"), json=bundle, headers=headers, timeout=args.timeout)
    except Exception as exc:
        print(f"ERROR posting Bundle to {args.base_url}: {exc}", file=sys.stderr)
        return 1

    print(f"Transaction HTTP status: {resp.status_code}")
    try:
        response_bundle = resp.json()
    except ValueError:
        print("Server did not return JSON.", file=sys.stderr)
        print(resp.text[:2000], file=sys.stderr)
        return 1

    rows = parse_bundle_response(response_bundle)
    accepted = sum(1 for r in rows if str(r["status"]).startswith(("200", "201")))

    # Persist the response and a readable summary.
    resp_path = os.path.join(args.output_dir, "bundle_response.json")
    with open(resp_path, "w", encoding="utf-8") as fh:
        json.dump(response_bundle, fh, indent=2)

    lines = [
        "FHIR Transaction Bundle Submission Report (AF-02)",
        "=" * 60,
        f"Generated : {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Server    : {args.base_url}",
        f"HTTP      : {resp.status_code}   |   Entries: {len(rows)}   |   Accepted: {accepted}",
        "=" * 60,
        "",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i:>2}. status {r['status']:<6} -> {r['server_ref'] or r['location'] or '-'}")
    report_path = os.path.join(args.output_dir, "bundle_report.txt")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"\n{accepted}/{len(rows)} entries accepted by the server in one transaction.")
    print(f"Response saved to {resp_path}")
    print(f"Summary saved to  {report_path}")

    if response_bundle.get("resourceType") == "OperationOutcome":
        print("\nServer returned an OperationOutcome (transaction may have been rejected):")
        for issue in response_bundle.get("issue", []):
            print(f"  - [{issue.get('severity')}] {issue.get('diagnostics', '')}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
