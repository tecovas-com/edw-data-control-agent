#!/usr/bin/env python3
"""Grant (or revoke) read-only BigQuery access for the WFX agent.

Implements issue #6, reusing the EXISTING shared service account
edw-data-control-agent@<project> (the recovery agent's identity) rather than
creating a dedicated one. This script GRANTS BigQuery read to that SA but never
creates or deletes it.

The grants are scoped so a hallucinated or malicious query physically cannot read
off-allowlist data, write, or exfiltrate. The boundary is enforced at the IAM/ACL
layer, NOT in application code:

  * roles/bigquery.jobUser at the PROJECT
        -> may RUN query jobs (billing). Grants ZERO data access on its own.
  * READER (== bigquery.dataViewer) on each ALLOWED dataset, via the dataset's
    legacy ACL
        -> may READ those datasets only. This dataset list IS the allowlist.
        (These datasets use legacy ACLs, not IAM-policy mode, so
         `bq add-iam-policy-binding` is a no-op here — we edit the ACL directly.)
  * authorized-dataset links so VIEWS in a consuming dataset can read a source
    dataset on the caller's behalf
        -> lets the dbt_views staging VIEWS resolve over raw_wfx WITHOUT granting
           the SA any direct access to raw_wfx.

What this deliberately does NOT grant: dataEditor/dataOwner (=> all DML/DDL fails)
and any GCS permission (=> `EXPORT DATA OPTIONS(uri='gs://...')` exfil fails).

Idempotent — safe to re-run. --teardown removes ONLY the grants this script adds
(the shared SA itself is left intact). Preview with --dry-run. Inspect the
resulting grants with --verify.

Requirements: an authenticated `gcloud`/`bq` (Google Cloud SDK) with rights to
manage project IAM and the target datasets. No extra Python packages — this shells
out to the Google CLIs so its behavior matches what you'd run by hand.

Usage:
    python scripts/provision_bq_readonly_sa.py --verify
    python scripts/provision_bq_readonly_sa.py --dry-run
    python scripts/provision_bq_readonly_sa.py            # apply (asks to confirm)
    python scripts/provision_bq_readonly_sa.py --yes      # apply, no prompt
    python scripts/provision_bq_readonly_sa.py --teardown # reverse everything
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time

# --- configuration ----------------------------------------------------------
# Edit these to change the allowlist; the dataset list below IS the data boundary.

PROJECT = "tecovas-prod-edw"
# Pre-existing, SHARED service account (the recovery agent's identity). This
# script GRANTS BigQuery read to it but never creates or deletes it.
SA_ID = "edw-data-control-agent"

# Datasets the SA may READ (granted READER on each). These hold the modeled data:
#   core  -> core__products (has_wfx_data, ...), core__sales, core__orders (tables)
#   base  -> base_products__wfx (SKU-grain wfx product/costing/vendor) (table)
#   dbt_views -> stg_wfx__* staging (VIEWS; need the authorized-dataset link below)
ALLOWED_DATASETS: tuple[str, ...] = ("core", "base", "dbt_views")

# Authorized-dataset links: (source_dataset, consuming_dataset).
# "VIEWS in <consuming_dataset> may read <source_dataset> on the caller's behalf."
# Lets the stg_wfx__* VIEWS in dbt_views resolve over raw_wfx without the SA
# itself getting any direct raw_wfx grant.
AUTHORIZED_DATASETS: tuple[tuple[str, str], ...] = (("raw_wfx", "dbt_views"),)

JOB_ROLE = "roles/bigquery.jobUser"
READER_ROLE = "READER"  # legacy-ACL equivalent of roles/bigquery.dataViewer
SA_EMAIL = f"{SA_ID}@{PROJECT}.iam.gserviceaccount.com"
SA_MEMBER = f"serviceAccount:{SA_EMAIL}"


# --- thin CLI wrappers ------------------------------------------------------


def _run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a CLI command, echoing it first."""
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        capture_output=capture,
    )


def _bq_get_dataset(dataset: str) -> dict:
    """Return a dataset's full metadata (including its `access` ACL list)."""
    out = _run(["bq", "show", "--format=prettyjson", f"{PROJECT}:{dataset}"]).stdout
    return json.loads(out)


def _bq_update_dataset(dataset: str, data: dict) -> None:
    """Write modified dataset metadata back (used to edit the `access` ACL)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(data, fh)
        path = fh.name
    _run(["bq", "update", "--source", path, f"{PROJECT}:{dataset}"])


# --- ACL entry helpers (pure) -----------------------------------------------


def _reader_entry() -> dict:
    return {"role": READER_ROLE, "userByEmail": SA_EMAIL}


def _authorized_dataset_entry(consuming: str) -> dict:
    return {
        "dataset": {
            "dataset": {"projectId": PROJECT, "datasetId": consuming},
            "targetTypes": ["VIEWS"],
        }
    }


def _has_reader(access: list[dict]) -> bool:
    return any(
        e.get("role") == READER_ROLE and e.get("userByEmail", "").lower() == SA_EMAIL.lower()
        for e in access
    )


def _has_authorized_dataset(access: list[dict], consuming: str) -> bool:
    for e in access:
        ds = e.get("dataset", {}).get("dataset", {})
        if ds.get("datasetId") == consuming and ds.get("projectId") == PROJECT:
            return True
    return False


# --- steps ------------------------------------------------------------------


def sa_exists() -> bool:
    return _run(
        ["gcloud", "iam", "service-accounts", "describe", SA_EMAIL, f"--project={PROJECT}"],
        check=False,
    ).returncode == 0


def verify_service_account(dry_run: bool) -> None:
    print("\n[1/4] service account (shared, pre-existing)")
    if sa_exists():
        print(f"  ok: using existing {SA_EMAIL}")
        return
    raise RuntimeError(
        f"service account {SA_EMAIL} does not exist; this script reuses an "
        f"existing SA and never creates one"
    )


def grant_job_user(dry_run: bool) -> None:
    print("\n[2/4] project-level jobUser (run queries; no data access)")
    if dry_run:
        print(f"  would grant {JOB_ROLE} to {SA_MEMBER} on {PROJECT}")
        return
    # add-iam-policy-binding is idempotent; retry while the SA finishes propagating.
    last = None
    for attempt in range(8):
        proc = _run([
            "gcloud", "projects", "add-iam-policy-binding", PROJECT,
            f"--member={SA_MEMBER}", f"--role={JOB_ROLE}", "--condition=None",
        ], check=False)
        if proc.returncode == 0:
            print("  granted")
            return
        last = (proc.stdout or "") + (proc.stderr or "")
        if "does not exist" in last:
            print(f"  attempt {attempt + 1}: SA not propagated yet, retrying...")
            time.sleep(3)
            continue
        break
    raise RuntimeError(f"failed to grant {JOB_ROLE}: {last}")


def grant_dataset_readers(dry_run: bool) -> None:
    print("\n[3/4] per-dataset READER (the allowlist)")
    for ds in ALLOWED_DATASETS:
        data = _bq_get_dataset(ds)
        access = data.setdefault("access", [])
        if _has_reader(access):
            print(f"  ok: READER on {ds} already present")
            continue
        if dry_run:
            print(f"  would add READER for {SA_EMAIL} on {ds}")
            continue
        access.append(_reader_entry())
        _bq_update_dataset(ds, data)
        print(f"  granted READER on {ds}")


def grant_authorized_datasets(dry_run: bool) -> None:
    print("\n[4/4] authorized-dataset links (views read source on caller's behalf)")
    for source, consuming in AUTHORIZED_DATASETS:
        data = _bq_get_dataset(source)
        access = data.setdefault("access", [])
        if _has_authorized_dataset(access, consuming):
            print(f"  ok: {consuming} already authorized on {source}")
            continue
        if dry_run:
            print(f"  would authorize VIEWS in {consuming} to read {source}")
            continue
        access.append(_authorized_dataset_entry(consuming))
        _bq_update_dataset(source, data)
        print(f"  authorized {consuming} -> {source}")


def verify() -> None:
    print(f"\n=== verify: grants for {SA_EMAIL} ===")
    print(f"\nSA exists: {sa_exists()}")

    print(f"\nproject IAM ({JOB_ROLE}):")
    proc = _run([
        "gcloud", "projects", "get-iam-policy", PROJECT,
        "--flatten=bindings[].members",
        f"--filter=bindings.members:{SA_MEMBER} AND bindings.role={JOB_ROLE}",
        "--format=value(bindings.role)",
    ], check=False)
    print("  present" if (proc.stdout or "").strip() else "  MISSING")

    print("\ndataset READER grants:")
    for ds in ALLOWED_DATASETS:
        access = _bq_get_dataset(ds).get("access", [])
        print(f"  {ds}: {'READER ok' if _has_reader(access) else 'MISSING'}")

    print("\nauthorized-dataset links:")
    for source, consuming in AUTHORIZED_DATASETS:
        access = _bq_get_dataset(source).get("access", [])
        ok = _has_authorized_dataset(access, consuming)
        print(f"  {consuming} -> {source}: {'ok' if ok else 'MISSING'}")


def teardown(dry_run: bool) -> None:
    print("\n=== teardown: removing all grants and the SA ===")

    print("\n[1/4] authorized-dataset links")
    for source, consuming in AUTHORIZED_DATASETS:
        data = _bq_get_dataset(source)
        access = data.get("access", [])
        kept = [e for e in access if not (e.get("dataset", {}).get("dataset", {}).get("datasetId") == consuming)]
        if len(kept) == len(access):
            print(f"  ok: nothing to remove on {source}")
            continue
        if dry_run:
            print(f"  would remove {consuming} authorization from {source}")
            continue
        data["access"] = kept
        _bq_update_dataset(source, data)
        print(f"  removed {consuming} authorization from {source}")

    print("\n[2/4] dataset READER grants")
    for ds in ALLOWED_DATASETS:
        data = _bq_get_dataset(ds)
        access = data.get("access", [])
        kept = [
            e for e in access
            if not (e.get("role") == READER_ROLE and e.get("userByEmail", "").lower() == SA_EMAIL.lower())
        ]
        if len(kept) == len(access):
            print(f"  ok: no READER to remove on {ds}")
            continue
        if dry_run:
            print(f"  would remove READER for {SA_EMAIL} on {ds}")
            continue
        data["access"] = kept
        _bq_update_dataset(ds, data)
        print(f"  removed READER on {ds}")

    print("\n[3/4] project jobUser binding")
    # Left intact: this is a shared SA that already held jobUser independently,
    # and jobUser grants ZERO data access on its own. Removing it could break the
    # SA's other uses. Revoke manually if you are certain nothing else needs it.
    print(f"  left intact: {JOB_ROLE} (shared; grants no data access by itself)")

    print("\n[4/4] service account")
    print(f"  left intact: {SA_EMAIL} is a shared, pre-existing SA and is never deleted")


# --- entrypoint -------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print actions without making changes")
    parser.add_argument("--verify", action="store_true", help="print the current grant state and exit")
    parser.add_argument("--teardown", action="store_true", help="reverse everything (remove grants, delete SA)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    print(f"project={PROJECT}  sa={SA_EMAIL}")
    print(f"allowlist={list(ALLOWED_DATASETS)}  authorized={[f'{c}->{s}' for s, c in AUTHORIZED_DATASETS]}")

    if args.verify:
        verify()
        return 0

    action = "TEAR DOWN" if args.teardown else "PROVISION"
    if not args.dry_run and not args.yes:
        reply = input(f"\n{action} the above against {PROJECT}? [y/N] ").strip().lower()
        if reply != "y":
            print("aborted")
            return 1

    try:
        if args.teardown:
            teardown(args.dry_run)
        else:
            verify_service_account(args.dry_run)
            grant_job_user(args.dry_run)
            grant_dataset_readers(args.dry_run)
            grant_authorized_datasets(args.dry_run)
            if not args.dry_run:
                verify()
    except subprocess.CalledProcessError as exc:
        print(f"\nERROR: command failed: {exc}\n{exc.stderr or ''}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
