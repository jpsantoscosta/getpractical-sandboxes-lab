"""Run bounded, evidence-producing experiments against a dedicated sandbox group."""

from __future__ import annotations

import argparse
import json
import math
import shlex
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Callable, Literal, TypeVar
from uuid import uuid4

from azure.containerapps.sandbox import (
    AddVolumeMountRequest,
    AutoDeletePolicy,
    AutoSuspendPolicy,
    EgressPolicy,
    EgressRule,
    EgressRuleAction,
    EgressRuleMatch,
    LifecyclePolicy,
    SandboxClient,
    SandboxGroupClient,
    SandboxVolume,
    endpoint_for_region,
)
from azure.core.exceptions import AzureError, HttpResponseError, ResourceNotFoundError
from azure.identity import AzureCliCredential

T = TypeVar("T")
ROOT = Path(__file__).resolve().parent
SDK_VERSION = "0.1.0b4"
SANDBOX_NAMES = {
    "core": "state-and-network-demo",
    "isolation-peer": "filesystem-isolation-check",
    "snapshot-clone": "restored-memory-demo",
    "volume-writer": "storage-writer",
    "volume-contender": "second-mount-check",
    "volume-reader": "storage-reader",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    required = (
        "lab_id", "subscription_id", "tenant_id", "resource_group",
        "sandbox_group", "region", "image",
    )
    if config.get("schema_version") != 1 or any(not config.get(k) for k in required):
        raise ValueError("The environment configuration is incomplete or has an unsupported schema.")
    if config.get("status") == "destroyed":
        raise ValueError("This lab environment has been destroyed.")
    if version("azure-containerapps-sandbox") != SDK_VERSION:
        raise ValueError(f"This lab requires azure-containerapps-sandbox=={SDK_VERSION}.")
    return config


def make_client(config: dict, credential: AzureCliCredential) -> SandboxGroupClient:
    # A timed-out exec might have completed. Do not replay mutations automatically.
    return SandboxGroupClient(
        endpoint_for_region(config["region"]),
        credential,
        subscription_id=config["subscription_id"],
        resource_group=config["resource_group"],
        sandbox_group=config["sandbox_group"],
        retry_total=0,
        connection_timeout=15,
        read_timeout=120,
    )


def egress_policy() -> EgressPolicy:
    return EgressPolicy(
        default_action="Deny",
        traffic_inspection="Full",
        rules=[
            EgressRule(
                name="github-zen-get",
                match=EgressRuleMatch(host="api.github.com", path="/zen", methods=["GET"]),
                action=EgressRuleAction(type="Allow"),
            )
        ],
    )


def lifecycle_policy(mode: Literal["Memory", "Disk"]) -> LifecyclePolicy:
    return LifecyclePolicy(
        auto_suspend=AutoSuspendPolicy(enabled=True, interval=300, mode=mode),
        auto_delete=AutoDeletePolicy(enabled=True, delete_interval_seconds=3600),
    )


def percentile(values: list[float], fraction: float) -> float:
    if not values or not 0 < fraction <= 1:
        raise ValueError("A percentile needs samples and a fraction in (0, 1].")
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def preflight(client: SandboxGroupClient, image: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        try:
            sandboxes = list(client.list_sandboxes())
            images = list(client.list_public_disk_images())
            break
        except HttpResponseError as error:
            if error.status_code != 403 or time.monotonic() >= deadline:
                raise
            print("Data-plane RBAC is not ready; retrying the read-only preflight in 10 seconds.", flush=True)
            time.sleep(10)
    matches = [item for item in images if item.name == image]
    if len(matches) != 1:
        names = ", ".join(sorted(item.name for item in images))
        raise RuntimeError(f"Required public image {image!r} was not found. Available images: {names}")
    return {"image": asdict(matches[0]), "existing_sandboxes": len(sandboxes)}


class Lab:
    def __init__(self, client: SandboxGroupClient, config: dict, directory: Path) -> None:
        self.client = client
        self.config = config
        self.run_id = uuid4().hex[:12]
        self.run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        self.snapshot_name = f"memory-checkpoint-{self.run_stamp}"
        self.directory = directory / f"lab-run-{self.run_stamp}"
        self.sandboxes: list[str] = []
        self.snapshots: list[str] = []
        self.volumes: list[str] = []
        self.report = {
            "schema_version": 1,
            "run_id": self.run_id,
            "started_utc": utc_now(),
            "region": config["region"],
            "sdk_version": SDK_VERSION,
            "image": config["image"],
            "status": "in_progress",
            "checks": [],
            "measurements": [],
            "cleanup_errors": [],
        }
        self.flush()

    def flush(self) -> None:
        write_json(self.directory / "report.json", self.report)
        write_json(self.directory / "resources.json", {
            "sandboxes": self.sandboxes,
            "snapshots": self.snapshots,
            "volumes": self.volumes,
        })

    def check(self, name: str, condition: bool, evidence: object) -> None:
        self.report["checks"].append({
            "name": name,
            "status": "passed" if condition else "failed",
            "evidence": evidence,
            "time_utc": utc_now(),
        })
        self.flush()
        print(f"{'PASS' if condition else 'FAIL'} {name}", flush=True)
        if not condition:
            raise RuntimeError(f"Check {name!r} failed; inspect {self.directory / 'report.json'}.")

    def measured(self, name: str, operation: Callable[[], T]) -> T:
        started = time.perf_counter()
        result = operation()
        self.report["measurements"].append({
            "operation": name,
            "seconds": time.perf_counter() - started,
        })
        self.flush()
        return result

    def execute(self, sandbox: SandboxClient, command: str, expected: int = 0) -> str:
        result = sandbox.exec(command)
        if result.exit_code != expected:
            raise RuntimeError(
                f"Command in {sandbox.sandbox_id} returned {result.exit_code}, expected {expected}. "
                f"stderr={result.stderr[:2000]!r}; stdout={result.stdout[:2000]!r}"
            )
        return result.stdout

    def python(self, sandbox: SandboxClient, code: str, expected: int = 0) -> str:
        return self.execute(sandbox, "timeout 30s python3 -c " + shlex.quote(code), expected)

    def create(
        self,
        purpose: str,
        *,
        mode: Literal["Memory", "Disk"] = "Memory",
        volumes: list[SandboxVolume] | None = None,
        snapshot_id: str | None = None,
        cpu: str = "1000m",
        memory: str = "2048Mi",
    ) -> SandboxClient:
        name = SANDBOX_NAMES.get(purpose, purpose.replace("benchmark-", "startup-check-"))
        print(f"Creating {name}", flush=True)
        started = time.perf_counter()
        if snapshot_id:
            # Only restore the trusted loopback fixture. This SDK cannot attach
            # an egress policy atomically to a create-from-snapshot request.
            poller = self.client.begin_create_sandbox(
                snapshot_id=snapshot_id, polling_timeout=180, polling_interval=2,
            )
        else:
            poller = self.client.begin_create_sandbox(
                disk=self.config["image"],
                cpu=cpu,
                memory=memory,
                disk_size="20Gi",
                labels={
                    "name": name, "lab": self.config["lab_id"],
                    "run": self.run_id, "purpose": purpose,
                },
                egress_policy=egress_policy(),
                auto_suspend_seconds=300,
                auto_suspend_mode=mode,
                volumes=volumes,
                polling_timeout=180,
                polling_interval=2,
            )
        sandbox = poller.result()
        ready = time.perf_counter()
        self.sandboxes.append(sandbox.sandbox_id)
        self.flush()
        output = self.execute(sandbox, "printf sandbox-ready")
        first_exec = time.perf_counter()
        if output != "sandbox-ready":
            raise RuntimeError("The first command did not return the expected readiness marker.")
        self.report["measurements"].append({
            "operation": "create",
            "purpose": purpose,
            "name": name,
            "sandbox_id": sandbox.sandbox_id,
            "create_to_running_seconds": ready - started,
            "create_to_first_exec_seconds": first_exec - started,
            "first_exec_roundtrip_seconds": first_exec - ready,
        })
        sandbox.set_lifecycle_policy(lifecycle_policy(mode))
        self.flush()
        return sandbox

    def delete(self, sandbox: SandboxClient) -> None:
        sandbox.begin_delete(polling_timeout=180, polling_interval=2).result()
        if sandbox.sandbox_id in self.sandboxes:
            self.sandboxes.remove(sandbox.sandbox_id)
        self.flush()

    def request(self, sandbox: SandboxClient, url: str) -> dict:
        code = f"""
import json
import urllib.error
import urllib.request
request = urllib.request.Request({url!r}, headers={{"User-Agent": "aca-sandboxes-lab"}})
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print(json.dumps({{"status": response.status, "error": None}}))
except urllib.error.HTTPError as error:
    print(json.dumps({{
        "status": error.code,
        "error": "HTTPError",
        "body": error.read(512).decode("utf-8", errors="replace")
    }}))
except (urllib.error.URLError, TimeoutError) as error:
    print(json.dumps({{"status": None, "error": str(error)}}))
"""
        return json.loads(self.python(sandbox, code))

    def state(self, sandbox: SandboxClient) -> dict:
        return json.loads(self.python(sandbox, """
import time
import urllib.error
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 15
while True:
    try:
        with opener.open("http://127.0.0.1:8765/state", timeout=2) as response:
            print(response.read().decode())
        break
    except (urllib.error.URLError, TimeoutError):
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.25)
"""))

    def start_state_server(self, sandbox: SandboxClient) -> dict:
        sandbox.write_file(
            "/tmp/aca-lab/state_probe.py",
            (ROOT / "guest" / "state_probe.py").read_text(encoding="utf-8"),
        )
        self.execute(
            sandbox,
            "nohup python3 /tmp/aca-lab/state_probe.py "
            "> /tmp/aca-lab/state-probe.log 2>&1 < /dev/null &",
        )
        return self.state(sandbox)

    def isolation(self, sandbox: SandboxClient) -> None:
        self.execute(sandbox, "command -v python3 && command -v timeout")
        inventory = json.loads(self.python(sandbox, """
import json, os, platform, shutil
from pathlib import Path
print(json.dumps({
    "python": platform.python_version(),
    "kernel": platform.release(),
    "machine": platform.machine(),
    "uid": os.getuid(),
    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    "root_disk_bytes": shutil.disk_usage("/").total
}))
"""))
        self.report["guest_inventory"] = inventory
        self.report["assigned_resources"] = asdict(sandbox.get().resources)
        token = uuid4().hex
        sandbox.write_file("/tmp/aca-lab/disk-token.txt", token)
        sibling = self.create("isolation-peer")
        peer = json.loads(self.python(sibling, """
import json
from pathlib import Path
print(json.dumps({
    "marker_present": Path("/tmp/aca-lab/disk-token.txt").exists(),
    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()
}))
"""))
        self.check("fresh_sandboxes_do_not_share_local_files", not peer["marker_present"], peer)
        self.check("fresh_boot_identifiers_differ", peer["boot_id"] != inventory["boot_id"], {
            "source": inventory["boot_id"], "peer": peer["boot_id"],
            "scope": "Behavioral observation, not a proof of hypervisor isolation.",
        })
        self.delete(sibling)

    def network(self, sandbox: SandboxClient, phase: str = "fresh") -> None:
        policy = sandbox.get_egress_policy()
        self.check(f"{phase}_deny_full_policy_readback", (
            policy.default_action == "Deny" and policy.traffic_inspection == "Full"
            and len(policy.rules) == 1 and policy.rules[0].match is not None
            and policy.rules[0].match.path == "/zen"
        ), asdict(policy))
        allowed = self.request(sandbox, "https://api.github.com/zen")
        self.check(f"{phase}_allowed_https_request_succeeds", allowed["status"] == 200, allowed)
        observations = {"allowed": allowed}
        evidence = {}
        for label, url, host, path in (
            ("blocked_host", "https://example.com/", "example.com", "/"),
            ("blocked_path", "https://api.github.com/", "api.github.com", "/"),
        ):
            deadline = time.monotonic() + 30
            responses = []
            while True:
                response = self.request(sandbox, url)
                responses.append(response)
                decisions = sandbox.get_egress_decisions()
                audit = decisions.network_egress
                denied = bool(audit and any(
                    item.host == host and item.path == path for item in audit.denied
                ))
                if denied or response["status"] == 200 or time.monotonic() >= deadline:
                    break
                time.sleep(2)
            observations[label] = responses
            evidence[label] = {"responses": responses, "audit": asdict(decisions)}
            write_json(self.directory / f"egress-decisions-{phase}.json", evidence)
            self.check(f"{phase}_{label}_has_deny_audit", (
                denied and all(item["status"] == 403 for item in responses)
            ), {"attempts": len(responses), "responses": responses})
        final_audit = sandbox.get_egress_decisions()
        allowed_seen = bool(final_audit.network_egress and any(
            item.host == "api.github.com" and item.path == "/zen"
            for item in final_audit.network_egress.allowed
        ))
        self.report.setdefault("http_observations", {})[phase] = observations
        self.report.setdefault("audit_observations", {})[phase] = {
            "allowed_request_in_audit": allowed_seen,
            "note": "HTTP success and policy readback are tested separately. The audit read API is not assumed to be a complete request history.",
        }
        if not allowed_seen:
            print("OBSERVATION: The successful request was absent from the audit read API; allow-event export is not verified.", flush=True)
        self.flush()

    def command_failures(self, sandbox: SandboxClient) -> None:
        failed = sandbox.exec("python3 -c 'raise SystemExit(42)'")
        self.check("nonzero_command_exit_is_preserved", failed.exit_code == 42, asdict(failed))
        timed = sandbox.exec("timeout 2s python3 -c 'import time; time.sleep(30)'")
        self.check("guest_timeout_kills_the_test_command", timed.exit_code == 124, asdict(timed))
        self.check("sandbox_works_after_command_timeout", (
            self.execute(sandbox, "printf still-running") == "still-running"
        ), {"expected": "still-running"})

    def checkpoint(self, sandbox: SandboxClient) -> None:
        before = self.start_state_server(sandbox)
        stopped = self.measured("memory_stop", lambda: sandbox.begin_stop(
            polling_timeout=180, polling_interval=2,
        ).result())
        self.check("memory_stop_reaches_stopped_state", stopped.state in ("Stopped", "Suspended", "Idle"), {
            "state": stopped.state,
        })
        self.measured("memory_resume", lambda: sandbox.begin_resume(
            polling_timeout=180, polling_interval=2,
        ).result())
        resumed = self.state(sandbox)
        self.check("memory_resume_preserves_process_state", (
            resumed["process_nonce"] == before["process_nonce"]
            and resumed["counter"] == before["counter"] + 1
            and resumed["disk_token"] == before["disk_token"]
            and resumed["boot_id"] == before["boot_id"]
        ), {"before": before, "after": resumed})

        snapshot = self.measured(
            "capture_snapshot",
            lambda: sandbox.create_snapshot(name=self.snapshot_name),
        )
        self.snapshots.append(snapshot.id)
        self.flush()
        deadline = time.monotonic() + 120
        while True:
            try:
                self.client.get_snapshot(snapshot.id)
                break
            except ResourceNotFoundError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        mutated_token = uuid4().hex
        sandbox.write_file("/tmp/aca-lab/disk-token.txt", mutated_token)
        clone = self.create("snapshot-clone", snapshot_id=snapshot.id)
        clone_state = self.state(clone)
        self.check("snapshot_clones_checkpoint_not_later_disk_writes", (
            clone_state["disk_token"] == resumed["disk_token"]
            and clone_state["disk_token"] != mutated_token
        ), {"checkpoint": resumed["disk_token"], "clone": clone_state["disk_token"]})
        self.check("snapshot_clone_restores_process_memory", (
            clone_state["process_nonce"] == resumed["process_nonce"]
            and clone_state["counter"] == resumed["counter"] + 1
        ), {"checkpoint": resumed, "clone": clone_state})
        clone.set_egress_policy(egress_policy())
        self.network(clone, phase="clone_after_explicit_policy")
        self.delete(clone)

        sandbox.set_lifecycle_policy(lifecycle_policy("Disk"))
        self.measured("disk_stop", lambda: sandbox.begin_stop(
            polling_timeout=180, polling_interval=2,
        ).result())
        self.measured("disk_resume", lambda: sandbox.begin_resume(
            polling_timeout=180, polling_interval=2,
        ).result())
        preserved = sandbox.read_file("/tmp/aca-lab/disk-token.txt").decode()
        listener = json.loads(self.python(sandbox, """
import json, socket
with socket.socket() as connection:
    connection.settimeout(2)
    result = connection.connect_ex(("127.0.0.1", 8765))
print(json.dumps({"listener_present": result == 0}))
"""))
        self.check("disk_resume_preserves_files_not_listener", (
            preserved == mutated_token and not listener["listener_present"]
        ), {"disk_token_matches": preserved == mutated_token, **listener})
        restarted = self.start_state_server(sandbox)
        self.check("disk_resume_requires_new_process", (
            restarted["process_nonce"] != before["process_nonce"]
            and restarted["boot_id"] != before["boot_id"]
        ), {"original": before, "restarted": restarted})

    def persistent_volume(self) -> None:
        name = f"working-data-{self.run_stamp}"
        self.volumes.append(name)
        self.flush()
        self.client.create_volume(
            name, type="DataDisk", size="1Gi",
            labels={"lab": self.config["lab_id"], "run": self.run_id},
        )
        mount = SandboxVolume(volume_name=name, mountpoint="/mnt/lab")
        writer = self.create(
            "volume-writer", mode="Disk", volumes=[mount], cpu="2000m", memory="4096Mi",
        )
        token = uuid4().hex
        self.python(writer, f"""
import os
with open("/mnt/lab/persisted.txt", "w") as output:
    output.write({token!r})
    output.flush()
    os.fsync(output.fileno())
""")
        contender = self.create("volume-contender", mode="Disk", cpu="2000m", memory="4096Mi")
        rejection = None
        try:
            contender.add_volume_mount(AddVolumeMountRequest(volume_name=name, mountpoint="/mnt/lab"))
        except HttpResponseError as error:
            rejection = {"status": error.status_code, "message": str(error)[:1500]}
        self.check("data_disk_rejects_second_concurrent_mount", (
            rejection is not None and rejection["status"] in (400, 409)
            and any(word in rejection["message"].lower() for word in ("attach", "mount", "in use"))
        ), rejection)
        self.delete(contender)
        self.delete(writer)
        reader = self.create(
            "volume-reader", mode="Disk", volumes=[mount], cpu="2000m", memory="4096Mi",
        )
        actual = reader.read_file("/mnt/lab/persisted.txt").decode()
        self.check("data_disk_survives_sandbox_deletion", actual == token, {"content_matches": actual == token})
        self.delete(reader)
        self.client.begin_delete_volume(name, polling_timeout=180, polling_interval=2).result()
        self.volumes.remove(name)
        self.flush()

    def benchmark(self, samples: int) -> None:
        for index in range(samples):
            sandbox = self.create(f"benchmark-{index + 1:02d}")
            self.delete(sandbox)
        observations = [
            row for row in self.report["measurements"]
            if row["operation"] == "create" and row["purpose"].startswith("benchmark-")
        ]
        self.report["benchmark"] = {
            "samples": len(observations),
            "concurrency": 1,
            "polling_interval_seconds": 2,
            "percentile_method": "nearest rank; for five samples p95 is the maximum",
            "create_to_running_seconds": {
                "p50": percentile([r["create_to_running_seconds"] for r in observations], 0.5),
                "p95": percentile([r["create_to_running_seconds"] for r in observations], 0.95),
            },
            "create_to_first_exec_seconds": {
                "p50": percentile([r["create_to_first_exec_seconds"] for r in observations], 0.5),
                "p95": percentile([r["create_to_first_exec_seconds"] for r in observations], 0.95),
            },
        }
        self.flush()

    def cleanup(self) -> None:
        errors = self.report["cleanup_errors"]
        try:
            for sandbox in self.client.list_sandboxes(
                labels={"lab": self.config["lab_id"], "run": self.run_id},
            ):
                if sandbox.id not in self.sandboxes:
                    self.sandboxes.append(sandbox.id)
            for snapshot in self.client.list_snapshots():
                if snapshot.labels.get("name") == self.snapshot_name and snapshot.id not in self.snapshots:
                    self.snapshots.append(snapshot.id)
        except (AzureError, TimeoutError, RuntimeError) as error:
            errors.append(f"Resource discovery failed: {error}")
        for sandbox_id in list(reversed(self.sandboxes)):
            try:
                self.client.begin_delete_sandbox(
                    sandbox_id, polling_timeout=180, polling_interval=2,
                ).result()
                self.sandboxes.remove(sandbox_id)
            except ResourceNotFoundError:
                self.sandboxes.remove(sandbox_id)
            except (AzureError, TimeoutError, RuntimeError) as error:
                errors.append(f"Sandbox {sandbox_id}: {error}")
        for snapshot_id in list(self.snapshots):
            try:
                self.client.begin_delete_snapshot(
                    snapshot_id, polling_timeout=180, polling_interval=2,
                ).result()
                self.snapshots.remove(snapshot_id)
            except ResourceNotFoundError:
                self.snapshots.remove(snapshot_id)
            except (AzureError, TimeoutError, RuntimeError) as error:
                errors.append(f"Snapshot {snapshot_id}: {error}")
        for name in list(self.volumes):
            try:
                self.client.begin_delete_volume(name, polling_timeout=180, polling_interval=2).result()
                self.volumes.remove(name)
            except ResourceNotFoundError:
                self.volumes.remove(name)
            except (AzureError, TimeoutError, RuntimeError) as error:
                errors.append(f"Volume {name}: {error}")
        self.flush()
        for error in errors:
            print(f"CLEANUP FAILED: {error}", file=sys.stderr)

    def run(self, samples: int) -> None:
        print(f"Evidence directory: {self.directory}", flush=True)
        completed = False
        try:
            self.report["preflight"] = preflight(self.client, self.config["image"])
            sandbox = self.create("core")
            self.isolation(sandbox)
            self.network(sandbox)
            self.command_failures(sandbox)
            self.checkpoint(sandbox)
            self.delete(sandbox)
            self.persistent_volume()
            self.benchmark(samples)
            completed = True
        except (AzureError, RuntimeError, TimeoutError, OSError, ValueError) as error:
            self.report["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            self.cleanup()
            self.report["finished_utc"] = utc_now()
            self.report["status"] = (
                "passed" if completed and not self.report["cleanup_errors"] else "failed"
            )
            self.flush()
        if self.report["cleanup_errors"]:
            raise RuntimeError("The experiments completed but runtime cleanup failed.")
        print(f"Lab passed. Report: {self.directory / 'report.json'}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "run"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=5, choices=range(1, 21))
    args = parser.parse_args()
    config = load_config(args.config)
    credential = AzureCliCredential(
        subscription=config["subscription_id"],
        process_timeout=30,
    )
    with credential, make_client(config, credential) as client:
        if args.action == "preflight":
            result = preflight(client, config["image"])
            print(f"Data-plane access verified; image {result['image']['name']} is available.")
        else:
            Lab(client, config, args.config.resolve().parent / "reports").run(args.samples)


if __name__ == "__main__":
    main()
