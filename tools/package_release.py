"""Package this checkout without caches, environments, credentials or test data."""
import hashlib
import json
from pathlib import Path
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def _anonymous(info):
    # Archives must not carry the builder's local account name.
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def main():
    version = json.loads((ROOT / "extension/manifest.json").read_text())["version"]
    host_version = tomllib.loads((ROOT / "native-host/pyproject.toml").read_text())["project"]["version"]
    assert host_version == version
    DIST.mkdir(exist_ok=True)
    xpi = DIST / f"ytdlp-downloader-{version}.xpi"
    if not xpi.exists(): raise SystemExit("Run npm run build first.")
    files = [ROOT / name for name in (".gitignore", "README.md", "CHANGELOG.md", "VALIDATION.md", "LICENSE",
                                     "package.json", "package-lock.json")]
    for directory in ("extension", "native-host", "native-messaging-manifests", "tools", ".github"):
        for path in (ROOT / directory).rglob("*"):
            if not path.is_file(): continue
            if any(part in ("__pycache__", "build", "dist", ".build-venv") or part.endswith(".egg-info") for part in path.parts): continue
            if path.suffix in (".pyc", ".pyo", ".spec"): continue
            files.append(path)
    prefix = f"ytdlp-downloader-{version}"
    with zipfile.ZipFile(DIST / f"{prefix}-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files): archive.write(path, f"{prefix}/{path.relative_to(ROOT).as_posix()}")
    with tarfile.open(DIST / f"ytdlp-host-linux-{version}.tar.gz", "w:gz") as archive:
        for path in sorted(files): archive.add(path, arcname=f"{prefix}/{path.relative_to(ROOT).as_posix()}", filter=_anonymous)
    with zipfile.ZipFile(xpi) as archive:
        assert "manifest.json" in archive.namelist()
        assert not any("test-ui" in name for name in archive.namelist())
    checksums = []
    artifacts = [xpi, DIST / f"{prefix}-source.zip", DIST / f"ytdlp-host-linux-{version}.tar.gz"]
    windows = DIST / "ytdlp-host-windows.zip"
    if windows.exists(): artifacts.append(windows)
    for path in sorted(artifacts):
        if path.is_file():
            checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
            print(f"{path.name}: {path.stat().st_size:,} bytes")
    (DIST / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")


if __name__ == "__main__": main()
