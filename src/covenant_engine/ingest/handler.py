"""AWS wrapper: S3 upload -> intake bundle -> "IntakeReady" event for the spec agent.

Flow (not deployed yet; runs the same code as the local CLI):
  1. A PDF lands in s3://<bucket>/incoming/<borrower>/<file>.pdf
  2. S3 -> EventBridge "Object Created" rule invokes this Lambda (container image: PyMuPDF is native)
  3. run() builds the bundle in /tmp, uploads it to s3://<bucket>/intake/<borrower>/<stem>/
  4. Puts an "IntakeReady" event on the bus with the report summary; a rule starts the
     AgentCore spec agent with the bundle location. Undecided items are in the payload so the
     agent (or a person) sees them first.
"""
import json
import os
from pathlib import Path

import boto3

from .run import run

s3 = boto3.client("s3")
events = boto3.client("events")
BUS = os.environ.get("EVENT_BUS", "default")


def handler(event, context):
    detail = event["detail"]
    bucket, key = detail["bucket"]["name"], detail["object"]["key"]
    parts = key.split("/")                                 # incoming/<borrower>/<file>.pdf
    if len(parts) < 3 or not key.lower().endswith(".pdf"):
        return {"skipped": key}
    borrower = parts[1]
    local = Path("/tmp") / parts[-1]
    s3.download_file(bucket, key, str(local))

    out, report = run(local, borrower, out_root=Path("/tmp/intake"))
    prefix = f"intake/{borrower}/{out.name}/"
    for f in out.iterdir():
        s3.upload_file(str(f), bucket, prefix + f.name)

    summary = {
        "bucket": bucket, "bundle": prefix, "source_key": key, "sha256": report["sha256"],
        "doc_type": report["document"]["doc_type"],
        "contains_full_agreement": report["document"].get("contains_full_agreement"),
        "covenant_sections": report["covenant_sections"],
        "missing_attachments": report["attachments_referenced_in_packet_but_missing"],
        "undecided": report["undecided"],
    }
    # Only full agreements go to the spec agent; financials and certificates go to the testing flow later.
    detail_type = "IntakeReady" if summary["contains_full_agreement"] else "IntakeFiled"
    events.put_events(Entries=[{"Source": "covenant.intake", "DetailType": detail_type,
                                "Detail": json.dumps(summary), "EventBusName": BUS}])
    return summary
