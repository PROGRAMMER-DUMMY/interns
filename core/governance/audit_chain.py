"""Tamper-evident, hash-chained audit log for compliance-grade event recording.

Design
------
Each appended entry forms a SHA-256 hash chain:

    entry_hash = sha256(prev_hash + canonical_json(record))

where ``canonical_json`` is ``json.dumps(record, sort_keys=True,
separators=(",", ":"))`` and the first record chains from a fixed genesis
constant (``GENESIS_HASH``).

The chain file is a plain JSONL file; each line is one JSON object::

    {
        "seq":        <int, 0-based>,
        "timestamp":  <ISO-8601 UTC>,
        "prev_hash":  <hex string>,
        "entry_hash": <hex string>,
        "record":     <arbitrary dict>
    }

Security note
-------------
SHA-256 provides *integrity / tamper-detection*: anyone who mutates a line,
inserts a line, or reorders lines will break the chain and ``verify_chain``
will report ``ok=False`` with the first broken sequence number.

This is **not** non-repudiation against an attacker who can (a) read the file
and (b) recompute hashes — they could forge a valid-looking chain.  The next
step for true non-repudiation is HMAC-with-secret (shared key known only to
the recording process) or asymmetric signing (private key held offline or in
HSM).  Both are additive and do not require changing this file's format.

No external dependencies: stdlib only (hashlib, json, pathlib, datetime).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GENESIS_HASH: str = "0" * 64  # fixed sentinel; first record chains from this
CHAIN_VERSION: int = 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_audit_record(chain_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append *record* to the hash-chain at *chain_path* and return the entry.

    The chain file is created (including parent directories) if absent.
    The append is done with a single ``file.write`` call so that concurrent
    writers on most POSIX file-systems get at-least-once line atomicity; a
    full distributed lock is out of scope for this stdlib-only module.

    Parameters
    ----------
    chain_path:
        Path to the ``.jsonl`` chain file.  Need not exist yet.
    record:
        Arbitrary JSON-serialisable dict to record.  The caller owns its
        schema; audit_chain is schema-agnostic.

    Returns
    -------
    The full entry dict that was written (``{seq, timestamp, prev_hash,
    entry_hash, record}``).
    """
    chain_path.parent.mkdir(parents=True, exist_ok=True)

    prev_hash, seq = _tail(chain_path)
    canonical = _canonical_json(record)
    entry_hash = _sha256(prev_hash + canonical)
    entry: dict[str, Any] = {
        "seq": seq,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
        "record": record,
    }
    line = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
    with chain_path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return entry


def verify_chain(chain_path: Path) -> dict[str, Any]:
    """Verify the integrity of the chain at *chain_path*.

    Re-walks the file, recomputes every ``entry_hash`` from
    ``prev_hash + canonical_json(record)``, and checks:

    * (a) Each entry's ``prev_hash`` equals the previous entry's
      ``entry_hash`` (linkage).
    * (b) The recomputed hash matches the stored ``entry_hash`` (content
      integrity).
    * (c) ``seq`` is contiguous starting from 0 (no insertions / deletions).

    An empty or missing file is treated as a valid empty chain
    (``ok=True, entry_count=0``).

    Returns
    -------
    A dict::

        {
            "ok":               bool,
            "entry_count":      int,
            "first_broken_seq": int | None,
            "reason":           str,
        }
    """
    if not chain_path.exists():
        return {"ok": True, "entry_count": 0, "first_broken_seq": None, "reason": "empty chain"}

    lines = [ln for ln in chain_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return {"ok": True, "entry_count": 0, "first_broken_seq": None, "reason": "empty chain"}

    expected_prev = GENESIS_HASH
    for idx, raw_line in enumerate(lines):
        # --- parse ---
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "entry_count": idx,
                "first_broken_seq": idx,
                "reason": f"JSON parse error at line {idx}: {exc}",
            }

        # --- seq contiguity ---
        stored_seq = entry.get("seq")
        if stored_seq != idx:
            return {
                "ok": False,
                "entry_count": idx,
                "first_broken_seq": idx,
                "reason": (
                    f"seq discontinuity at position {idx}: expected {idx}, got {stored_seq!r}"
                ),
            }

        # --- prev_hash linkage ---
        stored_prev = entry.get("prev_hash", "")
        if stored_prev != expected_prev:
            return {
                "ok": False,
                "entry_count": idx,
                "first_broken_seq": idx,
                "reason": (
                    f"prev_hash mismatch at seq {idx}: "
                    f"expected {expected_prev!r}, got {stored_prev!r}"
                ),
            }

        # --- content integrity ---
        stored_hash = entry.get("entry_hash", "")
        record = entry.get("record", {})
        canonical = _canonical_json(record)
        recomputed = _sha256(expected_prev + canonical)
        if recomputed != stored_hash:
            return {
                "ok": False,
                "entry_count": idx,
                "first_broken_seq": idx,
                "reason": (
                    f"entry_hash mismatch at seq {idx}: "
                    f"stored {stored_hash!r} != recomputed {recomputed!r}"
                ),
            }

        expected_prev = stored_hash

    return {
        "ok": True,
        "entry_count": len(lines),
        "first_broken_seq": None,
        "reason": f"chain intact ({len(lines)} entries)",
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """``verify-audit-chain`` console script.

    Usage::

        verify-audit-chain --workspace workspaces/my-project
        verify-audit-chain --chain-path /abs/path/to/audit_chain.jsonl

    Exits 0 if the chain is intact, 1 if tampered or unreadable.
    """
    parser = argparse.ArgumentParser(
        prog="verify-audit-chain",
        description="Verify the tamper-evident audit chain for a workspace.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--workspace",
        metavar="WORKSPACE",
        help="Relative workspace path (e.g. workspaces/my-project); "
        "chain is read from <workspace>/interns/state/audit_chain.jsonl",
    )
    group.add_argument(
        "--chain-path",
        metavar="PATH",
        help="Absolute or relative path to the .jsonl chain file.",
    )
    args = parser.parse_args(argv)

    if args.chain_path:
        chain_path = Path(args.chain_path)
    else:
        # Resolve workspace relative to cwd (mirrors how other CLI tools work)
        chain_path = Path(args.workspace) / "interns" / "state" / "audit_chain.jsonl"

    result = verify_chain(chain_path)

    status_label = "[ok]" if result["ok"] else "[x]"
    print(
        f"{status_label} audit chain: {result['reason']} "
        f"(entries={result['entry_count']}, "
        f"chain={chain_path})"
    )
    sys.exit(0 if result["ok"] else 1)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _canonical_json(obj: Any) -> str:
    """Return a deterministic, compact JSON representation of *obj*."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(data: str) -> str:
    """Return the hex-encoded SHA-256 digest of UTF-8-encoded *data*."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _tail(chain_path: Path) -> tuple[str, int]:
    """Return ``(last_entry_hash, next_seq)`` by reading the tail of the chain.

    Fast path: read only the last non-empty line so we don't load the entire
    file into memory for large chains.
    """
    if not chain_path.exists():
        return GENESIS_HASH, 0

    last_line: str = ""
    with chain_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                last_line = stripped

    if not last_line:
        return GENESIS_HASH, 0

    try:
        entry = json.loads(last_line)
    except json.JSONDecodeError:
        # Corrupted tail — let verify_chain surface the error; here we just
        # continue appending from an estimated position.
        return GENESIS_HASH, 0

    return entry.get("entry_hash", GENESIS_HASH), entry.get("seq", -1) + 1
