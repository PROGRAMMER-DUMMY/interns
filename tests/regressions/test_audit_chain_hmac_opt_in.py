"""Production-readiness fix, P4: core/governance/audit_chain.py's hash chain
used plain SHA-256 -- the module's own docstring already names the residual
risk: "not non-repudiation against an attacker who can read the file and
recompute hashes" (docs/core_audit/PROD_SECURITY_GAPS.md Gap 6). A determined
attacker with write access can rewrite the whole chain and it verifies clean.

Fixed: `_entry_digest` uses HMAC-SHA256 with `AUTORESEARCH_AUDIT_HMAC_KEY`
when that env var is set -- an attacker who can write the file but not read
the key cannot forge a valid chain. Backward-compatible by construction: with
no key set, behavior is byte-identical to plain SHA-256 (today's behavior).
`append_audit_record` and `verify_chain` must agree on the key, or
verification fails closed (a mixed-mode/wrong-key chain reports ok=False),
never silently passes.

See ~/.claude/plans/dynamic-cooking-firefly.md P4.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.governance.audit_chain import append_audit_record, verify_chain


class NoKeyBackwardCompatTests(unittest.TestCase):
    def test_no_env_var_behaves_exactly_like_plain_sha256(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("AUTORESEARCH_AUDIT_HMAC_KEY", None)
            with tempfile.TemporaryDirectory() as tmp:
                chain = Path(tmp) / "audit_chain.jsonl"
                append_audit_record(chain, {"event": "start", "value": 1})
                result = verify_chain(chain)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["entry_count"], 1)


class HmacOptInTests(unittest.TestCase):
    def test_same_key_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            with patch.dict("os.environ", {"AUTORESEARCH_AUDIT_HMAC_KEY": "secret-key-1"}):
                append_audit_record(chain, {"event": "start"})
                append_audit_record(chain, {"event": "step_2"})
                result = verify_chain(chain)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["entry_count"], 2)

    def test_verifying_under_a_different_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            with patch.dict("os.environ", {"AUTORESEARCH_AUDIT_HMAC_KEY": "secret-key-1"}):
                append_audit_record(chain, {"event": "start"})
            with patch.dict("os.environ", {"AUTORESEARCH_AUDIT_HMAC_KEY": "different-key"}):
                result = verify_chain(chain)
            self.assertFalse(result["ok"], result)

    def test_hmac_written_chain_fails_closed_under_plain_sha256(self):
        # A chain written with a key, later read back with the key unset
        # (e.g. a misconfigured deployment), must not silently "downgrade"
        # and report a forged-looking chain as valid.
        with tempfile.TemporaryDirectory() as tmp:
            chain = Path(tmp) / "audit_chain.jsonl"
            with patch.dict("os.environ", {"AUTORESEARCH_AUDIT_HMAC_KEY": "secret-key-1"}):
                append_audit_record(chain, {"event": "start"})
            import os
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("AUTORESEARCH_AUDIT_HMAC_KEY", None)
                result = verify_chain(chain)
            self.assertFalse(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
