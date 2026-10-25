"""The dashboard image must contain every repo module the dashboard imports.

Two regressions this guards against: .dockerignore excluding a directory the Dockerfile
copies (the build fails), and the Dockerfile copying a hand-picked subset that misses a module
the app imports (the container crashes at startup). Neither shows up in the unit tests.
"""
import fnmatch
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def copy_sources() -> list[str]:
    sources = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            parts = [p for p in line.split()[1:] if not p.startswith("--")]
            sources += parts[:-1]
    return sources


def ignored(path: str) -> bool:
    """Docker-style matching: later patterns win, '!' re-includes, a 'dir/' pattern covers its contents."""
    result = False
    for raw in (ROOT / ".dockerignore").read_text().splitlines():
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negate = pattern.startswith("!")
        pattern = pattern.lstrip("!").rstrip("/")
        parts = path.split("/")
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        if any(fnmatch.fnmatch(p, pattern) for p in prefixes) or any(fnmatch.fnmatch(part, pattern) for part in parts):
            result = not negate
    return result


def covered(path: str, sources: list[str]) -> bool:
    return any(path == s or path.startswith(s.rstrip("/") + "/") or fnmatch.fnmatch(path, s) for s in sources)


class DockerImageContentsTests(unittest.TestCase):
    def test_copied_sources_are_not_dockerignored(self):
        for source in copy_sources():
            for path in (sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(source)) or [source]):
                self.assertFalse(ignored(path), f"Dockerfile copies {path}, but .dockerignore excludes it")

    def test_every_module_the_dashboard_imports_is_in_the_image(self):
        probe = ("import sys, json; import dashboard.backend.app; "
                 "print(json.dumps(sorted(m.__file__ for m in list(sys.modules.values()) if getattr(m, '__file__', None))))")
        out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True)
        files = [Path(f) for f in __import__("json").loads(out.stdout.strip().splitlines()[-1])]
        repo_modules = sorted(str(f.relative_to(ROOT)) for f in files if ROOT in f.parents and ".venv" not in f.parts
                              and "site-packages" not in f.parts and f.parts[len(ROOT.parts)] != "deploy")
        self.assertTrue(any(m.startswith("data/sources/") for m in repo_modules))
        sources = copy_sources()
        missing = [m for m in repo_modules if not covered(m, sources) or ignored(m)]
        self.assertEqual(missing, [], "modules imported by the dashboard but absent from the image")


if __name__ == "__main__":
    unittest.main()
