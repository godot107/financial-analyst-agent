"""Checks run against the deployed service by 05_verify.sh.

Requests are signed with SigV4 using the AWS CLI profile in AWS_PROFILE, exactly
as a real caller would sign them.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

URL = os.environ["FA_URL"].rstrip("/")
API_KEY = os.environ["FA_API_KEY"]
REGION = os.environ["FA_REGION"]
CREDENTIALS = boto3.Session().get_credentials()


def call(method, path, body=None, api_key=None, signed=True):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    if signed:
        request = AWSRequest(method=method, url=URL + path, data=data, headers=headers)
        SigV4Auth(CREDENTIALS, "lambda", REGION).add_auth(request)
        headers = dict(request.headers)
    try:
        with urllib.request.urlopen(
            urllib.request.Request(URL + path, data=data, headers=headers, method=method), timeout=60
        ) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as failed:
        return failed.code, failed.read().decode()[:200]


def check(label, got, expected):
    ok = got == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: HTTP {got}" + ("" if ok else f" (expected {expected})"))
    return ok


def main() -> int:
    memo = "--memo" in sys.argv
    results = []

    print("\nWho can reach it")
    status, _ = call("GET", "/v1/health", signed=False)
    results.append(check("unsigned request (anyone on the internet)", status, 403))
    status, _ = call("POST", "/v1/memos", {"ticker": "MSFT", "question": "How liquid is it?"}, signed=True)
    results.append(check("signed, but no API key", status, 401))
    status, health = call("GET", "/v1/health")
    results.append(check("signed health check", status, 200))
    if status == 200:
        print(f"        spent today ${health['spent_today_usd']:.4f} of ${health['global_daily_cap_usd']:.2f}")

    print("\nFree route (EDGAR only, no model)")
    status, coverage = call("GET", "/v1/coverage/MSFT", api_key=API_KEY)
    results.append(check("coverage for MSFT", status, 200))

    if memo:
        print("\nOne real memo (~$0.05)")
        status, accepted = call(
            "POST", "/v1/memos", {"ticker": "MSFT", "question": "How liquid is Microsoft?"}, api_key=API_KEY
        )
        results.append(check("submitted", status, 202))
        if status == 202:
            started = time.time()
            while time.time() - started < 300:
                _, job = call("GET", accepted["status_url"], api_key=API_KEY)
                if job["status"] in ("done", "failed"):
                    break
                time.sleep(5)
            print(f"        {job['status']} after {time.time() - started:.0f}s, ${job['cost_usd']:.4f}")
            if job.get("error"):
                print(f"        {job['error']}")
            results.append(job["status"] == "done")

    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
