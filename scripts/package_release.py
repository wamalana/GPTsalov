"""Package source (not databases), verify CRC, and test the extracted release."""
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED


def main():
    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 scripts/package_release.py /absolute/new-release.zip")
    target = Path(sys.argv[1])
    if not target.is_absolute():
        raise SystemExit("Output path must be absolute")
    # Deliberate allowlist excludes runtime DBs, caches, credentials and local data.
    top = {"README.md", "DEPLOY.md", "VALIDATION.md", "config.toml", "pyproject.toml", "Dockerfile", "compose.yaml", ".gitignore", ".dockerignore"}
    files = sorted(p for p in root.rglob("*") if p.is_file() and
                   ((p.parent == root and p.name in top) or
                    (p.relative_to(root).parts[0] in {"gptsalov", "tests", "scripts"} and p.suffix == ".py")))
    with ZipFile(target, "x", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "gptsalov/"+path.relative_to(root).as_posix())
    with ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise SystemExit("ZIP CRC verification failed")
        with tempfile.TemporaryDirectory(prefix="gptsalov-release-check-") as temp:
            archive.extractall(temp)
            subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                           cwd=Path(temp)/"gptsalov", check=True)
    print(f"VERIFIED: {len(files)} source files; {target.stat().st_size} bytes; {target}")


if __name__ == "__main__":
    main()
