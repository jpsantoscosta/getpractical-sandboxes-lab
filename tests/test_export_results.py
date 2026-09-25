import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.export_results import EXPECTED_CHECKS, NETWORK_PHASES, results_summary


def successful_report() -> dict:
    before = {
        "process_nonce": "synthetic-process-a", "boot_id": "synthetic-boot-a",
        "pid": 10, "counter": 1,
    }
    checks = [{"name": name, "status": "passed", "evidence": {}} for name in EXPECTED_CHECKS]
    by_name = {item["name"]: item for item in checks}
    by_name["memory_resume_preserves_process_state"]["evidence"] = {
        "before": before, "after": {**before, "counter": 2},
    }
    by_name["disk_resume_requires_new_process"]["evidence"] = {
        "original": before,
        "restarted": {
            **before, "process_nonce": "synthetic-process-b", "boot_id": "synthetic-boot-b",
        },
    }
    by_name["data_disk_rejects_second_concurrent_mount"]["evidence"] = {
        "status": 409, "message": "synthetic raw service detail",
    }
    return {
        "schema_version": 1,
        "status": "passed",
        "cleanup_errors": [],
        "started_utc": "2026-09-25T00:00:00+00:00",
        "finished_utc": "2026-09-25T00:01:00+00:00",
        "region": "westeurope",
        "sdk_version": "0.1.0b4",
        "image": "python-3.12",
        "checks": checks,
        "guest_inventory": {
            "python": "3.12.14", "kernel": "synthetic-kernel", "machine": "x86_64",
            "uid": 0, "root_disk_bytes": 20 * 1024**3,
        },
        "assigned_resources": {"cpu": "1000m", "memory": "2048Mi", "disk": "20480Mi"},
        "http_observations": {phase: {
            "allowed": {"status": 200},
            "blocked_host": [{"status": 403}],
            "blocked_path": [{"status": 403}],
        } for phase in NETWORK_PHASES},
        "audit_observations": {phase: {
            "allowed_request_in_audit": False,
        } for phase in NETWORK_PHASES},
        "measurements": [{
            "operation": "create", "purpose": "benchmark-01", "name": "startup-check-01",
            "create_to_running_seconds": 0.75, "create_to_first_exec_seconds": 0.875,
            "first_exec_roundtrip_seconds": 0.125,
        }],
        "benchmark": {
            "samples": 1, "concurrency": 1, "polling_interval_seconds": 2,
            "percentile_method": "nearest rank",
            "create_to_running_seconds": {"p50": 0.75, "p95": 0.75},
            "create_to_first_exec_seconds": {"p50": 0.875, "p95": 0.875},
        },
    }


class ExportResultsTests(unittest.TestCase):
    def setUp(self):
        self.report = successful_report()
        self.digest = hashlib.sha256(b"synthetic report").hexdigest()

    def test_export_has_only_the_public_schema(self):
        result = results_summary(self.report, self.digest)
        self.assertEqual(set(result), {
            "schema_version", "provenance", "source_report_sha256", "started_utc",
            "finished_utc", "region", "sdk_version", "image", "status", "checks",
            "guest", "assigned_resources", "network", "memory_resume", "disk_resume",
            "second_data_disk_mount_http_status", "measurements", "benchmark", "cleanup_errors",
        })
        self.assertEqual(result["source_report_sha256"], self.digest)
        self.assertEqual(result["provenance"]["kind"], "curated_run_summary")
        self.assertEqual(result["memory_resume"], {
            "process_nonce_preserved": True, "pid_before": 10, "pid_after": 10,
            "counter_before": 1, "counter_after": 2, "boot_id_preserved": True,
        })
        self.assertEqual(result["disk_resume"], {"new_process_nonce": True, "new_boot_id": True})
        self.assertEqual(set(result["network"]), set(NETWORK_PHASES))
        self.assertEqual(result["second_data_disk_mount_http_status"], 409)
        self.assertEqual(result["measurements"], self.report["measurements"])
        self.assertEqual(result["benchmark"], self.report["benchmark"])
        self.assertTrue(all(set(check) == {"name", "status"} for check in result["checks"]))

    def test_unknown_diagnostics_and_nested_raw_fields_are_not_exported(self):
        marker = "synthetic-private-value"
        self.report["extra_observations"] = {"policy": marker, "labels": marker, "http": marker}
        self.report["run_id"] = marker
        self.report["guest_inventory"]["boot_id"] = marker
        self.report["assigned_resources"]["extra"] = marker
        self.report["measurements"][0]["sandbox_id"] = marker
        self.report["measurements"][0]["request_id"] = marker
        self.report["benchmark"]["extra"] = marker
        self.report["benchmark"]["create_to_running_seconds"]["extra"] = marker
        for check in self.report["checks"]:
            check["evidence"]["extra"] = marker
        for phase in NETWORK_PHASES:
            self.report["http_observations"][phase]["allowed"]["body"] = marker
            self.report["http_observations"][phase]["blocked_host"][0]["body"] = marker
            self.report["audit_observations"][phase]["extra"] = marker
        self.report["http_observations"]["extra"] = {"body": marker}
        result = results_summary(self.report, self.digest)
        self.assertNotIn(marker, json.dumps(result))
        self.assertEqual(set(result["assigned_resources"]), {"cpu", "memory", "disk"})
        self.assertEqual(set(result["benchmark"]), {
            "samples", "concurrency", "polling_interval_seconds", "percentile_method",
            "create_to_running_seconds", "create_to_first_exec_seconds",
        })
        self.assertEqual(set(result["benchmark"]["create_to_running_seconds"]), {"p50", "p95"})

    def test_export_does_not_change_its_input(self):
        original = copy.deepcopy(self.report)
        results_summary(self.report, self.digest)
        self.assertEqual(self.report, original)

    def test_export_refuses_failed_or_incomplete_results(self):
        for status, cleanup in (("failed", []), ("in_progress", []), ("passed", ["failed cleanup"])):
            with self.subTest(status=status, cleanup=cleanup):
                self.report["status"], self.report["cleanup_errors"] = status, cleanup
                with self.assertRaisesRegex(ValueError, "passed run"):
                    results_summary(self.report, self.digest)

    def test_export_requires_each_expected_check_once_and_passed(self):
        cases = (
            [],
            self.report["checks"][:-1],
            self.report["checks"] + [self.report["checks"][0]],
            self.report["checks"][:-1] + [self.report["checks"][0]],
            self.report["checks"][:-1] + [{"name": "unexpected", "status": "passed"}],
            self.report["checks"][:-1] + [{**self.report["checks"][-1], "status": "failed"}],
        )
        for checks in cases:
            with self.subTest(check_count=len(checks)):
                report = {**self.report, "checks": checks}
                with self.assertRaisesRegex(ValueError, "21 expected checks"):
                    results_summary(report, self.digest)

    def test_export_refuses_an_unknown_schema(self):
        self.report["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "Unsupported report schema"):
            results_summary(self.report, self.digest)

    def test_cli_hashes_the_actual_input_bytes_and_preserves_output_on_failure(self):
        scratch = ROOT / ".local" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            source = Path(temporary) / "report.json"
            output = Path(temporary) / "export.json"
            source.write_text(json.dumps(self.report), encoding="utf-8")
            command = [
                sys.executable, str(ROOT / "tools" / "export_results.py"),
                "--report", str(source), "--output", str(output),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            original = output.read_bytes()
            self.assertEqual(
                json.loads(original)["source_report_sha256"],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            self.report["status"] = "failed"
            source.write_text(json.dumps(self.report), encoding="utf-8")
            failed = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("passed run", failed.stderr)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
