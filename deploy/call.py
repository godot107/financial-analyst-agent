"""Call the deployed service over HTTPS, signed the way any real caller must sign.

Run it through ./deploy/call.sh, which finds the URL and your API key:

    ./deploy/call.sh GET /v1/health
    ./deploy/call.sh GET /v1/coverage/COST
    ./deploy/call.sh POST /v1/memos '{"ticker": "MSFT", "question": "How liquid is Microsoft?"}'
    ./deploy/call.sh wait <job id>          # poll until done, then print the memo and its trace
    ./deploy/call.sh --explain GET /v1/health   # show what signing adds to the request

Two separate locks, both required:
  1. AWS SigV4 signature: proves the caller holds AWS credentials allowed to invoke
     the function URL. Without it, AWS answers 403 before the code runs.
  2. X-API-Key header: says which caller this is, for the daily spending caps.
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fin_analyst.trace import TraceEvent, pretty  # noqa: E402

URL = os.environ["FA_URL"].rstrip("/")
API_KEY = os.environ["FA_API_KEY"]
REGION = os.environ["FA_REGION"]


def mask(value: str, keep: int = 6) -> str:
    return value[:keep] + "…" if len(value) > keep else "…"


def call(method: str, path: str, body: dict | None = None, explain: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json", "x-api-key": API_KEY}

    # The signature covers the method, path, headers and a hash of the body, so
    # none of them can be changed in transit. It is valid for 5 minutes.
    request = AWSRequest(method=method, url=URL + path, data=data, headers=headers)
    SigV4Auth(boto3.Session().get_credentials(), "lambda", REGION).add_auth(request)
    signed = dict(request.headers)

    if explain:
        print(f"{method} {URL + path}", file=sys.stderr)
        for name, value in signed.items():
            if name.lower() == "authorization":
                # e.g. AWS4-HMAC-SHA256 Credential=AKIA…/20260916/us-east-1/lambda/aws4_request,
                #      SignedHeaders=content-type;host;x-amz-date;x-api-key, Signature=…
                algorithm, _, rest = value.partition(" ")
                parts = dict(part.strip().split("=", 1) for part in rest.split(","))
                key_id, *scope = parts["Credential"].split("/")
                value = (
                    f"{algorithm} Credential={mask(key_id, 4)}/{'/'.join(scope)}, "
                    f"SignedHeaders={parts['SignedHeaders']}, Signature={mask(parts['Signature'])}"
                )
            elif name.lower() in ("x-api-key", "x-amz-security-token"):
                value = mask(value, 0)
            print(f"  {name}: {value}", file=sys.stderr)
        print(f"  (body sha256: {hashlib.sha256(data or b'').hexdigest()[:16]}…)\n", file=sys.stderr)

    try:
        with urllib.request.urlopen(
            urllib.request.Request(URL + path, data=data, headers=signed, method=method), timeout=60
        ) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as failed:
        text = failed.read().decode()
        try:
            return failed.code, json.loads(text)
        except ValueError:
            return failed.code, text


def wait(job_id: str, every: float = 5.0, limit: float = 300.0) -> int:
    started = time.time()
    while True:
        status, job = call("GET", f"/v1/memos/{job_id}")
        if status != 200:
            print(f"HTTP {status}: {job}", file=sys.stderr)
            return 1
        print(f"  {time.time() - started:4.0f}s  {job['status']}", file=sys.stderr)
        if job["status"] in ("done", "failed") or time.time() - started > limit:
            break
        time.sleep(every)

    trace = (job.get("result") or {}).get("trace", [])
    if trace:
        print("\n--- how it got there ---", file=sys.stderr)
        show = pretty(sys.stderr)
        for event in trace:
            show(TraceEvent.model_validate(event))
    print("\n--- memo ---" if job.get("memo") else f"\nNo memo: {job.get('error')}")
    if job.get("memo"):
        print(job["memo"])
    reused = (job.get("result") or {}).get("reused_from")
    print(f"\n[{job['status']}, ${job['cost_usd']:.4f}" + (f", reused from {reused}" if reused else "") + "]")
    return 0 if job["status"] == "done" else 1


def main(argv: list[str]) -> int:
    explain = "--explain" in argv
    argv = [a for a in argv if a != "--explain"]
    if len(argv) == 2 and argv[0] == "wait":
        return wait(argv[1])
    if len(argv) not in (2, 3) or argv[0].upper() not in ("GET", "POST"):
        print(__doc__, file=sys.stderr)
        return 2

    method, path = argv[0].upper(), argv[1]
    body = json.loads(argv[2]) if len(argv) == 3 else None
    status, payload = call(method, path, body, explain)
    print(f"HTTP {status}", file=sys.stderr)
    print(json.dumps(payload, indent=2))
    if status == 202 and isinstance(payload, dict) and "id" in payload:
        print(f"\nCollect it with: ./deploy/call.sh wait {payload['id']}", file=sys.stderr)
    return 0 if status < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
