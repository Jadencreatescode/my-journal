from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_bridge():
    package_name = f"journal_windows_bridge_{os.getpid()}_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(
        package_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    bridge_spec = importlib.util.spec_from_file_location(
        f"{package_name}.windows_bridge",
        ROOT / "windows_bridge.py",
    )
    assert bridge_spec is not None
    assert bridge_spec.loader is not None
    bridge = importlib.util.module_from_spec(bridge_spec)
    sys.modules[bridge_spec.name] = bridge
    bridge_spec.loader.exec_module(bridge)
    return bridge


class WindowsBridgeTests(unittest.TestCase):
    def test_native_cli_forwards_transactional_cron_setup_and_remove_to_wsl(self):
        bridge = load_bridge()
        setup = argparse.Namespace(
            journal_command="cron-setup", schedule="0 11 * * *", deliver="local",
            wsl_hermes_home="/home/exampleuser/.hermes",
        )
        remove = argparse.Namespace(
            journal_command="cron-remove", force_job_id="job-1",
            confirm="FORCE REMOVE MY JOURNAL CRON job-1",
        )
        with mock.patch.object(bridge, "_forward", return_value='{"ok":true}') as forward, mock.patch.object(
            bridge, "_write_wsl_runtime_config"
        ) as configure, mock.patch.object(
            bridge, "_bridge_paths", return_value={"windows_home": Path(r"C:\Hermes")}
        ), mock.patch("builtins.print"):
            self.assertEqual(bridge.journal_command(setup), 0)
            configure.assert_called_once_with(Path(r"C:\Hermes"), "/home/exampleuser/.hermes")
            forward.assert_called_once_with(
                "journal_internal_schedule_create",
                {"schedule": "0 11 * * *", "deliver": "local"},
            )
        with mock.patch.object(bridge, "_forward", return_value='{"ok":true}') as forward, mock.patch("builtins.print"):
            self.assertEqual(bridge.journal_command(remove), 0)
            forward.assert_called_once_with(
                "journal_internal_schedule_remove",
                {"force_job_id": "job-1", "confirmation": "FORCE REMOVE MY JOURNAL CRON job-1"},
            )

    def test_native_registration_forwards_every_tool_without_loading_linux_operations(self):
        bridge = load_bridge()
        environment = mock.patch.dict(
            os.environ,
            {"HERMES_HOME": r"C:\Hermes", "HERMES_EXECUTABLE": r"C:\Hermes\hermes.exe"},
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        calls: list[tuple[str, dict]] = []
        registered: list[dict] = []

        class Context:
            def register_tool(self, **kwargs):
                registered.append(kwargs)

            def register_cli_command(self, **kwargs):
                self.cli = kwargs

        context = Context()
        bridge.register_windows(
            context,
            invoker=lambda operation, args, **kwargs: calls.append((operation, args)) or {"ok": True},
        )

        self.assertEqual(len(registered), 16)
        self.assertEqual({item["name"] for item in registered}, set(bridge.TOOL_SCHEMAS))
        for item in registered:
            result = json.loads(item["handler"]({"probe": item["name"]}))
            self.assertEqual(result, {"ok": True})
        self.assertEqual(
            calls,
            [(item["name"], {"probe": item["name"]}) for item in registered],
        )
        self.assertEqual(context.cli["name"], "journal")

    def test_windows_package_import_selects_bridge_without_loading_linux_operations(self):
        package_name = f"journal_native_import_{os.getpid()}_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(
            package_name,
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        assert spec is not None
        assert spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = package
        with mock.patch.object(sys, "platform", "win32"):
            spec.loader.exec_module(package)
        self.assertEqual(package.register.__module__, f"{package_name}.windows_bridge")
        self.assertNotIn(f"{package_name}.operations", sys.modules)
        self.assertNotIn(f"{package_name}.onboarding", sys.modules)

    @unittest.skipIf(sys.platform == "win32", "Linux runtime tools intentionally require WSL")
    def test_platform_neutral_schemas_match_the_existing_runtime_contract(self):
        package_name = f"journal_schema_contract_{os.getpid()}_{time.time_ns()}"
        package_spec = importlib.util.spec_from_file_location(
            package_name,
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        assert package_spec is not None
        assert package_spec.loader is not None
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[package_name] = package
        package_spec.loader.exec_module(package)
        schemas_spec = importlib.util.spec_from_file_location(
            f"{package_name}.schemas", ROOT / "schemas.py"
        )
        assert schemas_spec is not None
        assert schemas_spec.loader is not None
        schemas = importlib.util.module_from_spec(schemas_spec)
        sys.modules[schemas_spec.name] = schemas
        schemas_spec.loader.exec_module(schemas)
        tools = importlib.import_module(f"{package_name}.tools")
        names = (
            "STATUS_SCHEMA", "RANGE_SCHEMA", "READ_SCHEMA", "GAPS_SCHEMA",
            "BACKFILL_SCHEMA", "SETUP_INVENTORY_SCHEMA",
            "SETUP_DATABASE_APPROVE_SCHEMA", "DAILY_WORKLOAD_CHECK_SCHEMA",
            "DAILY_WORKLOAD_APPROVE_SCHEMA", "SETUP_PLAN_SCHEMA",
            "SETUP_APPROVE_SCHEMA", "GENERATION_COLLECT_SCHEMA",
            "GENERATION_GET_CHUNK_SCHEMA", "GENERATION_RECORD_DIGEST_SCHEMA",
            "GENERATION_COMPLETE_SCHEMA",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(getattr(schemas, name), getattr(tools, name))

    def test_local_windows_paths_map_to_wsl_without_accepting_unc_or_relative_paths(self):
        bridge = load_bridge()

        self.assertEqual(
            bridge.windows_to_wsl_path(r"C:\Users\exampleuser\AppData\Local\hermes"),
            "/mnt/c/Users/exampleuser/AppData/Local/hermes",
        )
        self.assertEqual(
            bridge.windows_to_wsl_path(r"D:\Journal Data\entry.json"),
            "/mnt/d/Journal Data/entry.json",
        )
        for rejected in (r"relative\state.db", r"\\server\share\state.db", "", None):
            with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                bridge.windows_to_wsl_path(rejected)

    def test_wsl_request_uses_a_fixed_argument_vector_and_bounded_json_stdin(self):
        bridge = load_bridge()
        completed = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
        with mock.patch.object(
            bridge,
            "_configured_wsl_roots",
            return_value=(
                "/mnt/c/Users/exampleuser/AppData/Local/hermes",
                "/mnt/c/Users/exampleuser/AppData/Local/hermes/journal",
            ),
        ), mock.patch.object(bridge.subprocess, "run", return_value=completed) as run:
            result = bridge.invoke_wsl(
                "journal_status",
                {},
                windows_home=Path(r"C:\Users\exampleuser\AppData\Local\hermes"),
                plugin_root=Path(r"C:\Users\exampleuser\AppData\Local\hermes\plugins\my-journal"),
                windows_executable=Path(
                    r"C:\Users\exampleuser\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe"
                ),
                windows_python=Path(
                    r"C:\Users\exampleuser\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
                ),
            )

        self.assertEqual(result, {"ok": True})
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["wsl.exe", "-d", "Ubuntu", "--", "python3"])
        self.assertTrue(command[5].endswith("/plugins/my-journal/wsl_runtime.py"))
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "strict")
        self.assertLessEqual(len(run.call_args.kwargs["input"].encode("utf-8")), bridge.MAX_REQUEST_BYTES)
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(set(request), {
            "schema_version", "operation", "args", "windows_home",
            "windows_journal_root", "wsl_hermes_home", "wsl_journal_root",
            "windows_executable", "windows_python",
            "windows_plugin_root",
        })
        self.assertEqual(request["wsl_hermes_home"], "/mnt/c/Users/exampleuser/AppData/Local/hermes")
        self.assertEqual(request["wsl_journal_root"], "/mnt/c/Users/exampleuser/AppData/Local/hermes/journal")
        self.assertEqual(request["operation"], "journal_status")
        self.assertEqual(run.call_args.kwargs["timeout"], bridge._TIMEOUT_SECONDS)

    def test_config_store_uses_fixed_wsl_transport(self):
        bridge = load_bridge()
        completed = mock.Mock(returncode=0, stdout='{"exists":false}', stderr="")
        def mapped(value):
            text = str(value)
            if text == r"C:\Hermes":
                return "/mnt/c/Hermes"
            if text.endswith("wsl_config_store.py"):
                return "/mnt/c/Hermes/plugins/my-journal/wsl_config_store.py"
            raise AssertionError(text)

        with mock.patch.object(
            bridge, "windows_to_wsl_path", side_effect=mapped
        ), mock.patch.object(bridge.subprocess, "run", return_value=completed) as run:
            result = bridge._config_store_request("read", Path(r"C:\Hermes"))
        self.assertEqual(result, {"exists": False})
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["wsl.exe", "-d", "Ubuntu", "--", "python3"])
        self.assertTrue(command[5].endswith("/plugins/my-journal/wsl_config_store.py"))
        self.assertEqual(
            json.loads(run.call_args.kwargs["input"]),
            {"operation": "read", "root": "/mnt/c/Hermes"},
        )
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_persisted_wsl_home_selects_canonical_linux_journal(self):
        bridge = load_bridge()
        home = Path(r"C:\Hermes")
        with mock.patch.object(
            bridge,
            "_config_store_request",
            side_effect=[
                {"ok": True},
                {"exists": True, "wsl_hermes_home": "/home/exampleuser/.hermes"},
            ],
        ) as store:
            bridge._write_wsl_runtime_config(home, "/home/exampleuser/.hermes")
            self.assertEqual(
                bridge._configured_wsl_roots(home),
                ("/home/exampleuser/.hermes", "/home/exampleuser/.hermes/journal"),
            )
        self.assertEqual(store.call_args_list[0].args, ("write", home))
        self.assertEqual(store.call_args_list[0].kwargs, {"wsl_hermes_home": "/home/exampleuser/.hermes"})
        self.assertEqual(store.call_args_list[1].args, ("read", home))

    def test_setup_approval_uses_a_fixed_long_running_timeout(self):
        bridge = load_bridge()
        completed = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
        with mock.patch.object(
            bridge,
            "_configured_wsl_roots",
            return_value=(
                "/mnt/c/Users/exampleuser/AppData/Local/hermes",
                "/mnt/c/Users/exampleuser/AppData/Local/hermes/journal",
            ),
        ), mock.patch.object(bridge.subprocess, "run", return_value=completed) as run:
            bridge.invoke_wsl(
                "journal_setup_approve",
                {},
                windows_home=Path(r"C:\Users\exampleuser\AppData\Local\hermes"),
                plugin_root=Path(r"C:\Users\exampleuser\AppData\Local\hermes\plugins\my-journal"),
                windows_executable=Path(r"C:\Hermes\hermes.exe"),
                windows_python=Path(r"C:\Hermes\python.exe"),
            )

        self.assertEqual(
            run.call_args.kwargs["timeout"],
            bridge._SETUP_APPROVAL_TIMEOUT_SECONDS,
        )
        self.assertGreater(
            bridge._SETUP_APPROVAL_TIMEOUT_SECONDS,
            bridge._TIMEOUT_SECONDS,
        )

    def test_wsl_request_fails_closed_on_process_or_protocol_errors(self):
        bridge = load_bridge()
        common = {
            "windows_home": Path(r"C:\Users\exampleuser\AppData\Local\hermes"),
            "plugin_root": Path(r"C:\Users\exampleuser\AppData\Local\hermes\plugins\my-journal"),
            "windows_executable": Path(r"C:\Hermes\hermes.exe"),
            "windows_python": Path(r"C:\Hermes\python.exe"),
        }
        cases = (
            mock.Mock(returncode=1, stdout="{}", stderr="failed"),
            mock.Mock(returncode=0, stdout="not json", stderr=""),
            mock.Mock(returncode=0, stdout='["wrong shape"]', stderr=""),
        )
        for completed in cases:
            with self.subTest(completed=completed), mock.patch.object(
                bridge,
                "_configured_wsl_roots",
                return_value=(
                    "/mnt/c/Users/exampleuser/AppData/Local/hermes",
                    "/mnt/c/Users/exampleuser/AppData/Local/hermes/journal",
                ),
            ), mock.patch.object(
                bridge.subprocess, "run", return_value=completed
            ), self.assertRaises(ValueError):
                bridge.invoke_wsl("journal_status", {}, **common)


if __name__ == "__main__":
    unittest.main()
