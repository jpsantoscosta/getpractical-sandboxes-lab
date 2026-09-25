"""Build the public ZIP and SHA256SUMS locally from an explicit file allowlist."""

import argparse
import hashlib
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = "getpractical-sandboxes-lab"
ARCHIVE_NAME = f"{ARCHIVE_ROOT}.zip"
PUBLIC_FILES = (
    ".github/workflows/ci.yml",
    ".gitignore",
    "LICENSE",
    "README.md",
    "evidence/results.json",
    "guest/state_probe.py",
    "infra/main.bicep",
    "lab.ps1",
    "lab.py",
    "requirements.txt",
    "tests/test_export_results.py",
    "tests/test_lab.py",
    "tests/test_release.py",
    "tools/check_local.ps1",
    "tools/export_results.py",
    "tools/package_release.py",
)


def build_release(root: Path = ROOT) -> tuple[Path, str]:
    root = root.resolve(strict=True)
    if len(PUBLIC_FILES) != len(set(PUBLIC_FILES)):
        raise ValueError("The public-file allowlist contains duplicates.")
    payloads = []
    for relative in sorted(PUBLIC_FILES):
        path = PurePosixPath(relative)
        if (
            not path.parts or path.is_absolute() or ".." in path.parts
            or "\\" in relative or ":" in relative or path.as_posix() != relative
        ):
            raise ValueError(f"Not a canonical repository-relative public path: {relative}")
        source = root
        for part in path.parts:
            source = source / part
            if source.is_symlink() or source.is_junction():
                raise ValueError(f"Linked public inputs are not allowed: {relative}")
        if not source.is_file():
            raise FileNotFoundError(f"Missing public file: {relative}")
        payloads.append((relative, source.read_text(encoding="utf-8-sig").encode("utf-8")))

    output = root / "dist"
    if output.is_symlink() or output.is_junction():
        raise ValueError("The dist directory must not be a link.")
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".package-", dir=output) as temporary:
        archive = Path(temporary) / ARCHIVE_NAME
        with ZipFile(archive, "w") as package:
            for relative, data in payloads:
                entry = ZipInfo(f"{ARCHIVE_ROOT}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                package.writestr(entry, data, compress_type=ZIP_DEFLATED, compresslevel=9)
        with ZipFile(archive) as package:
            bad_file = package.testzip()
            if bad_file is not None:
                raise RuntimeError(f"Archive integrity check failed: {bad_file}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = Path(temporary) / "SHA256SUMS"
        checksum.write_text(f"{digest}  {ARCHIVE_NAME}\n", encoding="utf-8", newline="\n")
        archive.replace(output / ARCHIVE_NAME)
        checksum.replace(output / "SHA256SUMS")
    return output / ARCHIVE_NAME, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    archive, digest = build_release()
    print(archive)
    print(f"{digest}  {archive.name}")


if __name__ == "__main__":
    main()
