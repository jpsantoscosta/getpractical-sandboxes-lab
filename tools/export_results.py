"""Write an allowlisted local summary; review its values before publication."""

import argparse
import hashlib
import json
from pathlib import Path

EXPECTED_CHECKS = (
    "fresh_sandboxes_do_not_share_local_files",
    "fresh_boot_identifiers_differ",
    "fresh_deny_full_policy_readback",
    "fresh_allowed_https_request_succeeds",
    "fresh_blocked_host_has_deny_audit",
    "fresh_blocked_path_has_deny_audit",
    "nonzero_command_exit_is_preserved",
    "guest_timeout_kills_the_test_command",
    "sandbox_works_after_command_timeout",
    "memory_stop_reaches_stopped_state",
    "memory_resume_preserves_process_state",
    "snapshot_clones_checkpoint_not_later_disk_writes",
    "snapshot_clone_restores_process_memory",
    "clone_after_explicit_policy_deny_full_policy_readback",
    "clone_after_explicit_policy_allowed_https_request_succeeds",
    "clone_after_explicit_policy_blocked_host_has_deny_audit",
    "clone_after_explicit_policy_blocked_path_has_deny_audit",
    "disk_resume_preserves_files_not_listener",
    "disk_resume_requires_new_process",
    "data_disk_rejects_second_concurrent_mount",
    "data_disk_survives_sandbox_deletion",
)
NETWORK_PHASES = ("fresh", "clone_after_explicit_policy")


def results_summary(report: dict, digest: str) -> dict:
    if report.get("status") != "passed" or report.get("cleanup_errors") != []:
        raise ValueError("Only a passed run with successful cleanup can be exported.")
    if report["schema_version"] != 1:
        raise ValueError("Unsupported report schema.")
    checks = {item["name"]: item for item in report["checks"]}
    if (
        len(report["checks"]) != len(EXPECTED_CHECKS) or set(checks) != set(EXPECTED_CHECKS)
        or any(item["status"] != "passed" for item in checks.values())
    ):
        raise ValueError("Export requires all 21 expected checks to pass exactly once.")
    memory = checks["memory_resume_preserves_process_state"]["evidence"]
    disk = checks["disk_resume_requires_new_process"]["evidence"]
    network = {}
    for phase in NETWORK_PHASES:
        observations = report["http_observations"][phase]
        network[phase] = {
            "allowed_http_status": observations["allowed"]["status"],
            "blocked_host_http_statuses": [item["status"] for item in observations["blocked_host"]],
            "blocked_path_http_statuses": [item["status"] for item in observations["blocked_path"]],
            "allow_audit_observed": report["audit_observations"][phase]["allowed_request_in_audit"],
        }
    measurement_fields = (
        "operation", "purpose", "name", "seconds", "create_to_running_seconds",
        "create_to_first_exec_seconds", "first_exec_roundtrip_seconds",
    )
    measurements = [{
        key: row[key] for key in measurement_fields if key in row
    } for row in report["measurements"]]
    benchmark = report["benchmark"]
    return {
        "schema_version": 1,
        "provenance": {
            "kind": "curated_run_summary",
            "note": "A subset of one live report, not a validation of later code revisions.",
        },
        "source_report_sha256": digest,
        "started_utc": report["started_utc"],
        "finished_utc": report["finished_utc"],
        "region": report["region"],
        "sdk_version": report["sdk_version"],
        "image": report["image"],
        "status": report["status"],
        "checks": [{"name": name, "status": checks[name]["status"]} for name in EXPECTED_CHECKS],
        "guest": {
            key: report["guest_inventory"][key]
            for key in ("python", "kernel", "machine", "uid", "root_disk_bytes")
        },
        "assigned_resources": {
            key: report["assigned_resources"][key] for key in ("cpu", "memory", "disk")
        },
        "network": network,
        "memory_resume": {
            "process_nonce_preserved": memory["before"]["process_nonce"] == memory["after"]["process_nonce"],
            "pid_before": memory["before"]["pid"],
            "pid_after": memory["after"]["pid"],
            "counter_before": memory["before"]["counter"],
            "counter_after": memory["after"]["counter"],
            "boot_id_preserved": memory["before"]["boot_id"] == memory["after"]["boot_id"],
        },
        "disk_resume": {
            "new_process_nonce": disk["original"]["process_nonce"] != disk["restarted"]["process_nonce"],
            "new_boot_id": disk["original"]["boot_id"] != disk["restarted"]["boot_id"],
        },
        "second_data_disk_mount_http_status": checks["data_disk_rejects_second_concurrent_mount"]["evidence"]["status"],
        "measurements": measurements,
        "benchmark": {
            **{key: benchmark[key] for key in (
                "samples", "concurrency", "polling_interval_seconds", "percentile_method",
            )},
            **{key: {rank: benchmark[key][rank] for rank in ("p50", "p95")} for key in (
                "create_to_running_seconds", "create_to_first_exec_seconds",
            )},
        },
        "cleanup_errors": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = args.report.read_bytes()
    result = results_summary(json.loads(data), hashlib.sha256(data).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
