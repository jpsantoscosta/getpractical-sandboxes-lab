import ast
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab import SDK_VERSION, egress_policy, percentile
from tools.export_results import EXPECTED_CHECKS, NETWORK_PHASES
from tools.package_release import ARCHIVE_NAME, ARCHIVE_ROOT, PUBLIC_FILES, build_release


class ReleaseConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.results = json.loads((ROOT / "evidence" / "results.json").read_text(encoding="utf-8"))

    def test_historical_evidence_keeps_provenance_and_all_checks(self):
        self.assertEqual(self.results["status"], "passed")
        self.assertEqual([item["name"] for item in self.results["checks"]], list(EXPECTED_CHECKS))
        self.assertEqual(len(self.results["checks"]), 21)
        self.assertTrue(all(item["status"] == "passed" for item in self.results["checks"]))
        self.assertEqual(self.results["cleanup_errors"], [])
        self.assertEqual(self.results["provenance"]["kind"], "historical_curated")
        self.assertIn("not a fresh run", self.results["provenance"]["note"])
        self.assertTrue(self.results["started_utc"].startswith("2026-09-25T"))
        self.assertTrue(self.results["finished_utc"].startswith("2026-09-25T"))
        self.assertEqual(
            self.results["source_report_sha256"],
            "5db254479f517a2bed24e1161b0a2e2d4884e1f9b0e9ee4e94911b6d68b1a01c",
        )
        for phrase in ("all 21 checks passed", "historical curated", "not a fresh run", "25 September 2026"):
            self.assertIn(phrase, self.readme)

    def test_runner_assertion_names_match_the_export_contract(self):
        tree = ast.parse((ROOT / "lab.py").read_text(encoding="utf-8"))
        names = set()
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self" and call.func.attr == "check"
            ):
                continue
            name = call.args[0]
            if isinstance(name, ast.Constant):
                names.add(name.value)
                continue
            self.assertIsInstance(name, ast.JoinedStr)
            for phase, label in itertools.product(NETWORK_PHASES, ("blocked_host", "blocked_path")):
                values = {"phase": phase, "label": label}
                parts = []
                for part in name.values:
                    if isinstance(part, ast.Constant):
                        parts.append(part.value)
                    else:
                        self.assertIsInstance(part, ast.FormattedValue)
                        self.assertIsInstance(part.value, ast.Name)
                        parts.append(values[part.value.id])
                names.add("".join(parts))
        self.assertEqual(names, set(EXPECTED_CHECKS))

    def test_readme_timings_and_percentiles_match_the_samples(self):
        benchmark = self.results["benchmark"]
        creates = [row for row in self.results["measurements"] if row["operation"] == "create"]
        samples = [row for row in creates if row["purpose"].startswith("benchmark-")]
        self.assertEqual(len(creates), 11)
        self.assertEqual(len(samples), 5)
        self.assertEqual(benchmark["samples"], len(samples))
        self.assertEqual(benchmark["concurrency"], 1)
        self.assertIn("11 sandboxes", self.readme)
        self.assertIn("at most two concurrently", self.readme)
        for key in ("create_to_running_seconds", "create_to_first_exec_seconds"):
            p50, p95 = benchmark[key]["p50"], benchmark[key]["p95"]
            self.assertEqual(p50, percentile([sample[key] for sample in samples], 0.5))
            self.assertEqual(p95, percentile([sample[key] for sample in samples], 0.95))
            self.assertIn(f"| {p50:.3f} seconds | {p95:.3f} seconds |", self.readme)
        for row in self.results["measurements"]:
            if "seconds" in row:
                self.assertIn(f"| {row['seconds']:.3f} seconds |", self.readme)
            if row.get("purpose") == "snapshot-clone":
                self.assertIn(f"| {row['create_to_first_exec_seconds']:.3f} seconds |", self.readme)
        for row in creates:
            self.assertAlmostEqual(
                row["create_to_first_exec_seconds"],
                row["create_to_running_seconds"] + row["first_exec_roundtrip_seconds"],
            )

    def test_readme_policy_is_the_actual_create_policy(self):
        snippets = re.findall(r"```json\n(.*?)\n```", self.readme, flags=re.DOTALL)
        self.assertEqual(len(snippets), 1)
        self.assertEqual(json.loads(snippets[0]), egress_policy()._to_dict())

    def test_api_versions_pins_and_scope_stay_consistent(self):
        template = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("Microsoft.App/sandboxGroups@2026-07-01", template)
        self.assertIn("scope: sandboxGroup", template)
        self.assertIn("Microsoft.App/sandboxGroups@2026-07-01", self.readme)
        self.assertIn("2026-02-01-preview", self.readme)
        self.assertEqual(self.results["sdk_version"], SDK_VERSION)
        self.assertIn(f"azure-containerapps-sandbox=={SDK_VERSION}", self.readme)
        for line in (ROOT / "requirements.txt").read_text().splitlines():
            if line and not line.startswith("#"):
                name, pinned = line.split("==")
                self.assertEqual(version(name), pinned, name)

    def test_readme_relative_links_and_anchors_exist_without_private_documents(self):
        headings = {
            re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
            for heading in re.findall(r"^#+ (.+)$", self.readme, flags=re.MULTILINE)
        }
        for target in re.findall(r"\]\(([^)]+)\)", self.readme):
            link = urlsplit(target)
            if link.scheme:
                self.assertEqual(link.scheme, "https")
                continue
            if link.path:
                relative = unquote(link.path)
                self.assertIn(relative, PUBLIC_FILES, target)
                self.assertTrue((ROOT / relative).is_file(), target)
            elif link.fragment:
                self.assertIn(link.fragment, headings, target)
        self.assertNotRegex(self.readme, r"(?i)\bblog\b|local working draft")

    def test_download_layout_and_license_are_documented(self):
        repository = "https://github.com/jpsantoscosta/getpractical-sandboxes-lab"
        for url in (repository, f"{repository}/releases/latest", f"{repository}/releases/latest/download/{ARCHIVE_NAME}"):
            self.assertIn(url, self.readme)
        self.assertIn(f"Set-Location '.\\{ARCHIVE_ROOT}'", self.readme)
        self.assertIn("v1.0.0", self.readme)
        license_text = (ROOT / "LICENSE").read_text()
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2026 Joao Paulo Costa", license_text)

    def test_public_evidence_is_an_identifier_free_subset(self):
        serialized = json.dumps(self.results)
        for key in (
            "subscription_id", "tenant_id", "principal_id", "sandbox_id",
            "traceId", "requestId", "/subscriptions/",
        ):
            self.assertNotIn(key, serialized)
        self.assertTrue(all(set(item) == {"name", "status"} for item in self.results["checks"]))
        self.assertEqual(set(self.results["network"]), set(NETWORK_PHASES))
        for network in self.results["network"].values():
            self.assertEqual(set(network), {
                "allowed_http_status", "blocked_host_http_statuses",
                "blocked_path_http_statuses", "allow_audit_observed",
            })

    def test_public_text_has_no_personal_paths_or_unexpected_guids(self):
        allowed_guids = {
            "c24cf47c-5077-412d-a19c-45202126392c",
            "00000000-0000-0000-0000-000000000000",
        }
        for relative in PUBLIC_FILES:
            text = (ROOT / relative).read_text(encoding="utf-8-sig")
            self.assertNotRegex(text, r"(?i)(?:[a-z]:[\\/]+Users[\\/]+|[/]Users[/]|[/]home[/])", relative)
            guids = set(re.findall(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", text.lower()))
            self.assertLessEqual(guids, allowed_guids, relative)

    def test_ci_is_windows_local_only_and_read_only(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("permissions:\n  contents: read\n", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("python-version: ['3.12', '3.14']", workflow)
        self.assertIn(r"getpractical-sandboxes-lab\tools\check_local.ps1", workflow)
        for operation in ("azure/login", "upload-artifact", "release create", "git push", "pull_request_target"):
            self.assertNotIn(operation, workflow)


class PackageTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".local" / "tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for relative in PUBLIC_FILES:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / relative).read_bytes())

    def test_archive_allowlist_integrity_layout_and_checksum(self):
        archive, digest = build_release(self.root)
        with ZipFile(archive) as package:
            self.assertIsNone(package.testzip())
            self.assertEqual(package.namelist(), [f"{ARCHIVE_ROOT}/{name}" for name in sorted(PUBLIC_FILES)])
            for entry in package.infolist():
                self.assertEqual(PurePosixPath(entry.filename).parts[0], ARCHIVE_ROOT)
                self.assertNotIn("..", PurePosixPath(entry.filename).parts)
                self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(entry.external_attr >> 16, 0o100644)
                relative = entry.filename.removeprefix(f"{ARCHIVE_ROOT}/")
                expected = (self.root / relative).read_text(encoding="utf-8-sig").encode("utf-8")
                self.assertEqual(package.read(entry), expected)
            package.extractall(self.root / "expanded")
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), digest)
        self.assertEqual((archive.parent / "SHA256SUMS").read_bytes(), f"{digest}  {ARCHIVE_NAME}\n".encode())
        for relative in PUBLIC_FILES:
            self.assertTrue((self.root / "expanded" / ARCHIVE_ROOT / relative).is_file())

    def test_private_paths_and_unreviewed_files_are_never_packaged(self):
        marker = b"synthetic-" + b"not-for-publication"
        excluded = (
            "BLOG.md", ".local/environment.json", ".local/reports/run/report.json",
            ".local/reports/run/resources.json", ".local/reports/run/egress-decisions-fresh.json",
            ".venv/credentials.txt", ".git/config", ".env", "private/document.txt",
            "input-private.zip", "dist/release-notes-v1.0.0.md", "evidence/unreviewed.json",
        )
        for relative in excluded:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(marker)
        archive, _ = build_release(self.root)
        with ZipFile(archive) as package:
            for relative in excluded:
                self.assertNotIn(f"{ARCHIVE_ROOT}/{relative}", package.namelist())
            for entry in package.infolist():
                self.assertNotIn(marker, package.read(entry))

    def test_build_is_repeatable_across_mtime_and_line_endings(self):
        archive, first = build_release(self.root)
        original = archive.read_bytes()
        readme = self.root / "README.md"
        readme.write_bytes(readme.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
        os.utime(readme, (1_700_000_000, 1_700_000_000))
        archive, second = build_release(self.root)
        self.assertEqual(first, second)
        self.assertEqual(archive.read_bytes(), original)

    def test_missing_input_does_not_replace_a_previous_archive(self):
        archive, _ = build_release(self.root)
        original = archive.read_bytes()
        checksum = (archive.parent / "SHA256SUMS").read_bytes()
        (self.root / "LICENSE").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "Missing public file"):
            build_release(self.root)
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual((archive.parent / "SHA256SUMS").read_bytes(), checksum)

    def test_noncanonical_and_duplicate_allowlist_entries_are_rejected(self):
        for relative in ("../private.txt", "/private.txt", "tools\\private.txt", "./README.md", "tools//private.txt", "X:private.txt"):
            with self.subTest(path=relative), patch("tools.package_release.PUBLIC_FILES", (relative,)):
                with self.assertRaisesRegex(ValueError, "canonical"):
                    build_release(self.root)
        with patch("tools.package_release.PUBLIC_FILES", ("README.md", "README.md")):
            with self.assertRaisesRegex(ValueError, "duplicates"):
                build_release(self.root)

    def test_linked_files_and_parent_directories_are_rejected(self):
        for method, relative in (("is_symlink", "README.md"), ("is_junction", "tools")):
            with self.subTest(kind=method), patch.object(
                Path, method, autospec=True, side_effect=lambda path: path == self.root / relative,
            ):
                with self.assertRaisesRegex(ValueError, "Linked public inputs"):
                    build_release(self.root)

    def test_linked_output_directory_is_rejected(self):
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == self.root / "dist"):
            with self.assertRaisesRegex(ValueError, "dist directory"):
                build_release(self.root)


if __name__ == "__main__":
    unittest.main()
