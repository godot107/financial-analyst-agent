"""A cache for things that are expensive to fetch or to write, and safe to keep.

What makes a cache safe is the key. Here the keys are things that pin content
exactly, so a hit is never stale:

- **Filings** by accession number. An SEC filing never changes after it is
  accepted; an amendment is a different filing with its own accession number.
  So the facts and text of accession X are the same forever.
- **Prices** by ticker and trading day.
- **Memos** by a hash of everything that shapes one: the question, the options,
  the filings' accession numbers, and the code that writes and checks memos.
  Change a prompt and every old memo key stops matching.

Two stores, one interface, like the job ledger: a folder for local runs, an S3
prefix for Lambda. S3 because it has no standing charge and the service already
has a bucket; a cache server would bill every hour it sat idle.

No LLM here.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol

# Keys become file paths and object keys, so only a plain alphabet gets through.
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,300}$")


class Cache(Protocol):
    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, value: bytes) -> None: ...


def _check(key: str) -> str:
    if not SAFE_KEY.match(key) or ".." in key:
        raise ValueError(f"unsafe cache key: {key!r}")
    return key


class LocalCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def get(self, key: str) -> bytes | None:
        path = self.root / _check(key)
        return path.read_bytes() if path.is_file() else None

    def put(self, key: str, value: bytes) -> None:
        path = self.root / _check(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename, so a crash never leaves half a file that reads as a hit.
        partial = path.with_suffix(path.suffix + ".partial")
        partial.write_bytes(value)
        partial.replace(path)


class S3Cache:
    def __init__(self, bucket: str, client, prefix: str = "cache/"):
        self.bucket = bucket
        self.client = client
        self.prefix = prefix

    def get(self, key: str) -> bytes | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.prefix + _check(key))
        except Exception as missing:
            code = getattr(missing, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return None
            raise
        return response["Body"].read()

    def put(self, key: str, value: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.prefix + _check(key),
            Body=value,
            ContentType="application/json",
        )


class NoCache:
    """Caching switched off: every get misses, every put is dropped."""

    def get(self, key: str) -> bytes | None:
        return None

    def put(self, key: str, value: bytes) -> None:
        pass


# --- the code that shapes a memo ---------------------------------------------

# A change to any of these can change a memo's words, figures or checks.
MEMO_SOURCES = ("llm.py", "memo.py", "metrics.py", "graph.py", "passages.py", "news.py", "market.py", "edgar.py")


def code_version(package: Path | None = None) -> str:
    """A fingerprint of the code and config that write memos.

    Hashing the source rather than keeping a hand-bumped version number means a
    forgotten bump can never serve an old memo from new code.
    """
    package = package or Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in MEMO_SOURCES:
        digest.update(name.encode())
        digest.update((package / name).read_bytes())
    config = package.parent / "config.yaml"
    if config.is_file():
        digest.update(config.read_bytes())
    return digest.hexdigest()[:16]


def memo_key(request: dict, accessions: list[str], version: str, day: str | None = None) -> str:
    """The cache key for a finished memo.

    The question is compared after trimming and lower-casing only; a reworded
    question is a different question. `day` is included whenever the memo used
    something that changes daily (news, a share price).
    """
    material = {
        "request": {**request, "question": " ".join(str(request.get("question", "")).split()).lower()},
        "accessions": sorted(accessions),
        "code": version,
        "day": day,
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return f"memos/{digest}.json"
