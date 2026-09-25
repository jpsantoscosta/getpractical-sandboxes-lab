import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from azure.containerapps.sandbox import ExecResult, SandboxClient, SandboxGroupClient, SandboxVolume
from azure.core.exceptions import HttpResponseError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab import Lab, egress_policy, lifecycle_policy, make_client, percentile, preflight


class LabTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".local" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        transport = patch(
            "azure.core.pipeline.transport.RequestsTransport.send",
            side_effect=AssertionError("Local tests must not access the network."),
        )
        transport.start()
        self.addCleanup(transport.stop)
        self.config = {
            "lab_id": "unit-test-lab",
            "region": "westeurope",
            "image": "python-3.12",
        }
        credential = MagicMock()
        self.client = SandboxGroupClient(
            "https://management.westeurope.azuredevcompute.io",
            credential,
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg-unit-test",
            sandbox_group="sg-unit-test",
        )
        self.addCleanup(self.client.close)
        self.lab = Lab(self.client, self.config, Path(self.temporary.name))

    def test_policy_is_deny_full_with_one_path_and_method(self):
        wire = egress_policy()._to_dict()
        self.assertEqual(wire["defaultAction"], "Deny")
        self.assertEqual(wire["trafficInspection"], "Full")
        self.assertEqual(len(wire["rules"]), 1)
        self.assertEqual(wire["rules"][0]["match"], {
            "host": "api.github.com", "path": "/zen", "methods": ["GET"],
        })
        self.assertEqual(wire["rules"][0]["action"], {"type": "Allow"})

    def test_lifecycle_has_bounded_retention(self):
        wire = lifecycle_policy("Disk")._to_dict()
        self.assertEqual(wire["autoSuspendPolicy"], {"enabled": True, "interval": 300, "mode": "Disk"})
        self.assertEqual(wire["autoDeletePolicy"], {"enabled": True, "deleteIntervalInSeconds": 3600})

    def test_client_keeps_the_pinned_sdk_default_data_plane_api(self):
        config = {
            **self.config, "subscription_id": "00000000-0000-0000-0000-000000000000",
            "resource_group": "rg-unit-test", "sandbox_group": "sg-unit-test",
        }
        with make_client(config, MagicMock()) as client:
            self.assertEqual(client._api_version, "2026-02-01-preview")

    def test_real_sdk_create_serialization(self):
        with (
            patch.object(self.client, "_dp_put", return_value={"id": "sb-unit", "state": "Running"}) as put,
            patch.object(SandboxClient, "exec", return_value=ExecResult(stdout="sandbox-ready")),
            patch.object(SandboxClient, "set_lifecycle_policy"),
        ):
            self.lab.create("core")
        body = put.call_args.args[1]
        self.assertEqual(body["sourcesRef"], {"diskImage": {"name": "python-3.12", "isPublic": True}})
        self.assertEqual(body["resources"], {"cpu": "1000m", "memory": "2048Mi", "disk": "20Gi"})
        self.assertEqual(body["egressPolicy"], egress_policy()._to_dict())
        self.assertNotIn("ports", body)
        self.assertNotIn("environment", body)
        self.assertEqual(body["labels"]["lab"], self.config["lab_id"])
        self.assertEqual(body["labels"]["name"], "state-and-network-demo")
        self.assertEqual(self.lab.sandboxes, ["sb-unit"])

    def test_real_sdk_snapshot_restore_does_not_send_forbidden_overrides(self):
        with (
            patch.object(self.client, "_dp_put", return_value={"id": "sb-clone", "state": "Running"}) as put,
            patch.object(SandboxClient, "exec", return_value=ExecResult(stdout="sandbox-ready")),
            patch.object(SandboxClient, "set_lifecycle_policy"),
        ):
            self.lab.create("clone", snapshot_id="snapshot-unit")
        self.assertEqual(put.call_args.args[1], {"sourcesRef": {"snapshot": {"id": "snapshot-unit"}}})

    def test_volume_create_uses_disk_suspend_mode(self):
        with (
            patch.object(self.client, "_dp_put", return_value={"id": "sb-volume", "state": "Running"}) as put,
            patch.object(SandboxClient, "exec", return_value=ExecResult(stdout="sandbox-ready")),
            patch.object(SandboxClient, "set_lifecycle_policy"),
        ):
            self.lab.create(
                "writer", mode="Disk", cpu="2000m", memory="4096Mi",
                volumes=[SandboxVolume(volume_name="volume-unit", mountpoint="/mnt/lab")],
            )
        body = put.call_args.args[1]
        self.assertEqual(body["lifecycle"]["autoSuspendPolicy"]["mode"], "Disk")
        self.assertEqual(body["volumes"], [{"volumeName": "volume-unit", "mountpoint": "/mnt/lab"}])
        self.assertEqual(body["resources"]["cpu"], "2000m")
        self.assertEqual(body["resources"]["memory"], "4096Mi")
        self.assertEqual(body["resources"]["disk"], "20Gi")

    def test_checkpoint_keeps_state_checks_without_extra_clone_observations(self):
        sandbox = MagicMock(spec=SandboxClient)
        clone = MagicMock(spec=SandboxClient)
        sandbox.begin_stop.return_value.result.return_value.state = "Stopped"
        sandbox.create_snapshot.return_value.id = "snapshot-unit"
        sandbox.read_file.return_value = b"changed-token"
        before = {
            "process_nonce": "original-process", "counter": 1,
            "disk_token": "original-token", "boot_id": "original-boot",
        }
        resumed = {**before, "counter": 2}
        cloned = {**before, "counter": 3}
        restarted = {**before, "process_nonce": "new-process", "boot_id": "new-boot"}
        original_report_fields = set(self.lab.report)

        def check_network_after_update(target, phase):
            self.assertIs(target, clone)
            self.assertEqual(phase, "clone_after_explicit_policy")
            clone.set_egress_policy.assert_called_once()
            self.assertEqual(clone.set_egress_policy.call_args.args[0], egress_policy())

        with (
            patch("lab.uuid4", return_value=MagicMock(hex="changed-token")),
            patch.object(self.client, "get_snapshot"),
            patch.object(self.lab, "start_state_server", side_effect=[before, restarted]),
            patch.object(self.lab, "state", side_effect=[resumed, cloned]),
            patch.object(self.lab, "create", return_value=clone),
            patch.object(self.lab, "delete"),
            patch.object(self.lab, "python", return_value='{"listener_present": false}'),
            patch.object(self.lab, "request", side_effect=AssertionError("Unexpected outbound probe")),
            patch.object(self.lab, "network", side_effect=check_network_after_update) as network,
        ):
            self.lab.checkpoint(sandbox)
        network.assert_called_once_with(clone, phase="clone_after_explicit_policy")
        clone.get.assert_not_called()
        clone.get_egress_policy.assert_not_called()
        self.assertEqual(set(self.lab.report), original_report_fields)
        self.assertEqual([check["name"] for check in self.lab.report["checks"]], [
            "memory_stop_reaches_stopped_state", "memory_resume_preserves_process_state",
            "snapshot_clones_checkpoint_not_later_disk_writes", "snapshot_clone_restores_process_memory",
            "disk_resume_preserves_files_not_listener", "disk_resume_requires_new_process",
        ])
        self.assertTrue(all(check["status"] == "passed" for check in self.lab.report["checks"]))

    def test_command_failure_is_not_reported_as_success(self):
        sandbox = MagicMock(spec=SandboxClient)
        sandbox.sandbox_id = "sb-fail"
        sandbox.exec.return_value = ExecResult(exit_code=42, stderr="expected failure")
        with self.assertRaisesRegex(RuntimeError, "returned 42"):
            self.lab.execute(sandbox, "command")
        self.assertEqual(self.lab.execute(sandbox, "command", expected=42), "")

    def test_failed_check_persists_evidence_before_raising(self):
        with self.assertRaises(RuntimeError):
            self.lab.check("must-fail", False, {"reason": "unit test"})
        report = json.loads((self.lab.directory / "report.json").read_text())
        self.assertEqual(report["checks"][-1]["status"], "failed")

    def test_nearest_rank_percentile(self):
        self.assertEqual(percentile([5.0, 1.0, 3.0, 4.0, 2.0], 0.5), 3.0)
        self.assertEqual(percentile([5.0, 1.0, 3.0, 4.0, 2.0], 0.95), 5.0)
        with self.assertRaises(ValueError):
            percentile([], 0.95)

    def test_preflight_retries_only_rbac_403(self):
        client = MagicMock(spec=SandboxGroupClient)
        error = HttpResponseError("RBAC is propagating")
        error.status_code = 403
        client.list_sandboxes.side_effect = [error, []]
        client.list_public_disk_images.return_value = []
        with patch("lab.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "not found"):
                preflight(client, "missing")
        sleep.assert_called_once_with(10)
        error.status_code = 401
        client.list_sandboxes.side_effect = error
        with self.assertRaises(HttpResponseError):
            preflight(client, "missing")

    def test_cleanup_reconciles_labels_and_reports_failures(self):
        client = MagicMock(spec=SandboxGroupClient)
        client.list_sandboxes.return_value = []
        client.list_snapshots.return_value = []
        client.begin_delete_sandbox.side_effect = RuntimeError("delete did not complete")
        lab = Lab(client, self.config, Path(self.temporary.name))
        lab.sandboxes.append("sb-orphan")
        lab.cleanup()
        self.assertEqual(lab.sandboxes, ["sb-orphan"])
        self.assertIn("delete did not complete", lab.report["cleanup_errors"][0])
        client.list_sandboxes.assert_called_once_with(labels={
            "lab": self.config["lab_id"], "run": lab.run_id,
        })


if __name__ == "__main__":
    unittest.main()
