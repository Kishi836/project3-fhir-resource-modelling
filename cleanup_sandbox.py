#!/usr/bin/env python3
"""Purge duplicate identities from the shared HAPI FHIR sandbox.

Repeated ``--fresh`` demo runs (and the old create-fresh behaviour) leave many
Patient resources sharing the same ABHA -- and Practitioners sharing the same NMC
number -- on the public sandbox. That breaks the idempotent upsert path
(``validate_resources.py`` / ``submit_bundle.py`` default), because a conditional
update keyed on ``identifier`` then matches more than one resource and the server
rejects it (HAPI-0958 "matched N resources").

This script deletes every Patient with the scenario ABHA and every Practitioner
with the scenario NMC number, using HAPI cascade delete so their referencing
resources (Encounters, Observations, etc.) go with them. After running it once,
the next upsert run creates exactly one Patient/Practitioner and every later run
updates that same one.

Usage::

    python cleanup_sandbox.py --dry-run      # list what would be deleted
    python cleanup_sandbox.py                # delete duplicates (cascade)
    python cleanup_sandbox.py --base-url <url> --abha <id> --nmc <id>
"""

from __future__ import annotations

import argparse
import sys

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

DEFAULT_BASE_URL = "https://hapi.fhir.org/baseR4"
DEFAULT_ABHA = "https://abdm.gov.in/ABHA|1234-5678-9012-3456"
DEFAULT_NMC = "https://nmc.org.in/registration|NMC-KA-2009-45821"


def find_ids(base_url: str, rtype: str, identifier: str, timeout: int) -> list[str]:
    """Return all server ids of `rtype` matching ?identifier=<identifier> (all pages)."""
    headers = {"Accept": "application/fhir+json"}
    url = f"{base_url.rstrip('/')}/{rtype}?identifier={identifier}&_count=200&_elements=id"
    ids: list[str] = []
    pages = 0
    while url and pages < 50:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        bundle = resp.json()
        for entry in bundle.get("entry", []) or []:
            res = entry.get("resource") or {}
            if res.get("id"):
                ids.append(res["id"])
        url = next((l.get("url") for l in bundle.get("link", []) or [] if l.get("relation") == "next"), None)
        pages += 1
    return ids


def cascade_delete(base_url: str, rtype: str, server_id: str, timeout: int) -> tuple[int, str]:
    """Cascade-delete one resource. Returns (status_code, short message)."""
    url = f"{base_url.rstrip('/')}/{rtype}/{server_id}?_cascade=delete"
    headers = {"Accept": "application/fhir+json", "X-Cascade": "delete"}
    try:
        resp = requests.delete(url, headers=headers, timeout=timeout)
    except Exception as exc:
        return 0, f"ERROR {exc}"
    return resp.status_code, resp.reason or ""


def clean_type(base_url: str, rtype: str, identifier: str, dry_run: bool, timeout: int) -> int:
    ids = find_ids(base_url, rtype, identifier, timeout)
    print(f"\n{rtype} matching identifier={identifier}: {len(ids)} found")
    if not ids:
        return 0
    deleted = 0
    for sid in ids:
        if dry_run:
            print(f"  would delete {rtype}/{sid}")
            continue
        status, msg = cascade_delete(base_url, rtype, sid, timeout)
        ok = status in (200, 204)
        deleted += 1 if ok else 0
        print(f"  delete {rtype}/{sid} -> {status} {msg}")
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge duplicate Patient/Practitioner identities from a FHIR sandbox.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--abha", default=DEFAULT_ABHA, help="Patient identifier as system|value.")
    parser.add_argument("--nmc", default=DEFAULT_NMC, help="Practitioner identifier as system|value.")
    parser.add_argument("--dry-run", action="store_true", help="List what would be deleted, delete nothing.")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    if requests is None:
        print("ERROR: the 'requests' library is required (pip install -r requirements.txt).", file=sys.stderr)
        return 1

    print(f"Server: {args.base_url}   Mode: {'DRY RUN' if args.dry_run else 'DELETE (cascade)'}")
    try:
        clean_type(args.base_url, "Patient", args.abha, args.dry_run, args.timeout)
        clean_type(args.base_url, "Practitioner", args.nmc, args.dry_run, args.timeout)
    except Exception as exc:
        print(f"ERROR during cleanup: {exc}", file=sys.stderr)
        return 1

    if not args.dry_run:
        remaining = len(find_ids(args.base_url, "Patient", args.abha, args.timeout))
        print(f"\nPatients remaining with that ABHA: {remaining}")
        if remaining > 1:
            print("  (still >1 -- some deletes may have failed; re-run to retry.)")
    print("\nDone. The next upsert run will create exactly one Patient/Practitioner.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
