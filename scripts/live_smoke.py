"""Opt-in real HTTP API smoke test. Requires a running API + worker; never prints secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

DEFAULT_URLS = [
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    "https://x.com/captainamerica/status/719944021058060289",
    "https://www.instagram.com/reel/Chunk8-jurw/",
    "https://vimeo.com/22439234",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--url", action="append")
    parser.add_argument("--account-id", help="Explicit account for supplied --url values only")
    parser.add_argument("--output", default=".runtime/live-smoke.json")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    if args.account_id and not args.url:
        parser.error("--account-id requires explicit --url")
    load_dotenv()
    key = os.environ["DOWNLOADBOT_API_KEY"]
    rows = []
    started = time.monotonic()
    with httpx.Client(base_url=args.base_url, headers={"Authorization": f"Bearer {key}"},
                      timeout=30, trust_env=False) as client:
        client.get("/readyz").raise_for_status()
        for url in args.url or DEFAULT_URLS:
            body = {"url": url, "max_height": 360}
            if args.account_id:
                body["account_id"] = args.account_id
            response = client.post("/v1/jobs", json=body)
            response.raise_for_status()
            job = response.json()
            rows.append({"url": url, "job_id": job["id"], "platform": job["platform"], "status": job["status"]})
            print(f"Submitted {job['platform']} {job['id']}", flush=True)
        pending = {row["job_id"]: row for row in rows}
        while pending and time.monotonic() - started < args.timeout:
            for job_id, row in list(pending.items()):
                response = client.get(f"/v1/jobs/{job_id}")
                response.raise_for_status()
                job = response.json()
                if job["status"] in {"queued", "running"}:
                    continue
                row.update(status=job["status"], attempt=job["attempt"], error=job["error"],
                           title=job["title"], duration=job["duration"], size_bytes=job["size_bytes"])
                if job["status"] == "succeeded":
                    sha, count, prefix = hashlib.sha256(), 0, b""
                    with client.stream("GET", f"/v1/jobs/{job_id}/file") as media:
                        media.raise_for_status()
                        for chunk in media.iter_bytes():
                            sha.update(chunk)
                            count += len(chunk)
                            prefix = (prefix + chunk)[:16]
                    assert count == job["size_bytes"] and count > 0
                    assert b"ftyp" in prefix or prefix.startswith((b"\x1aE\xdf\xa3", b"FLV", b"\x47")), "Invalid media signature"
                    row.update(downloaded_bytes=count, sha256=sha.hexdigest(), media_signature=prefix.hex())
                print(f"Finished {row['platform']}: {row['status']} {row.get('error') or ''}", flush=True)
                del pending[job_id]
            if pending:
                time.sleep(1)
        for job_id, row in pending.items():
            client.post(f"/v1/jobs/{job_id}/cancel").raise_for_status()
            row.update(status="smoke_timeout", error={"code": "smoke_timeout", "message": "Smoke runner deadline exceeded"})
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "transport": "real HTTP API and worker",
              "elapsed_seconds": round(time.monotonic() - started, 2), "results": rows}
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Report: {destination}; {sum(r['status'] == 'succeeded' for r in rows)}/{len(rows)} downloaded", flush=True)
    return 0 if all(r["status"] == "succeeded" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
