from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "journal_config.py"
COLLECTOR = Path(__file__).parents[1] / "scripts" / "collect_journal.py"


def load_module():
    spec = importlib.util.spec_from_file_location("journal_config", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_collector():
    spec = importlib.util.spec_from_file_location("collect_journal", COLLECTOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def config_payload() -> dict:
    return {
        "schema_version": 1,
        "enabled": True,
        "timezone": "America/Los_Angeles",
        "profiles": ["default"],
        "platforms": ["discord"],
        "excluded_session_ids": [],
        "privacy": {
            "redact_secrets": True,
            "pii_mode": "mask",
            "entropy_mode": "report",
        },
        "limits": {
            "max_message_chars": 4000,
            "max_tool_chars": 1200,
            "max_selected_messages": 25000,
            "max_retained_chars": 4000000,
            "max_sessions": 2000,
            "packet_chunk_bytes": 120000,
            "max_packet_chunks": 64,
        },
    }


class JournalConfigTests(unittest.TestCase):
    def test_unimplemented_retention_policy_fails_closed(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = config_payload()
            config["retention"] = {"days": 30}
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported fields.*retention"):
                module.load_config(path)

    def write_config(self, root: Path, payload: dict) -> Path:
        path = root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_disabled_config_requires_explicit_consent(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            payload = config_payload()
            payload["enabled"] = False
            payload["profiles"] = []
            payload["platforms"] = []
            config = module.load_config(self.write_config(Path(tmp), payload))

            with self.assertRaisesRegex(ValueError, "explicit consent"):
                config.require_enabled()

    def test_enabled_config_requires_explicit_profile_and_platform_allowlists(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            for field in ("profiles", "platforms"):
                payload = config_payload()
                payload[field] = []
                with self.subTest(field=field):
                    with self.assertRaisesRegex(ValueError, field[:-1]):
                        module.load_config(self.write_config(Path(tmp), payload))

    def test_secret_redaction_cannot_be_disabled(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            payload = config_payload()
            payload["privacy"]["redact_secrets"] = False

            with self.assertRaisesRegex(ValueError, "mandatory"):
                module.load_config(self.write_config(Path(tmp), payload))

    def test_manifest_policy_records_scope_without_excluded_identifiers(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            payload = config_payload()
            payload["excluded_session_ids"] = ["private-session"]
            config = module.load_config(self.write_config(Path(tmp), payload))
            policy = config.manifest_policy()

            self.assertEqual(policy["profiles"], ["default"])
            self.assertEqual(policy["platforms"], ["discord"])
            self.assertEqual(policy["excluded_session_count"], 1)
            self.assertNotIn("private-session", json.dumps(policy))

    def test_collector_cli_refuses_disabled_config_without_artifacts(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            output = root / "journal"
            payload = config_payload()
            payload["enabled"] = False
            payload["profiles"] = []
            payload["platforms"] = []
            config_path = self.write_config(root, payload)

            stream = io.StringIO()
            with redirect_stdout(stream):
                code = collector.main([
                    "--home", str(home),
                    "--output", str(output),
                    "--config", str(config_path),
                    "--date", "1970-01-01",
                ])

            self.assertEqual(code, 1)
            self.assertFalse(output.exists())
            self.assertIn("explicit consent", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
