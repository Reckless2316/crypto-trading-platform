"""WAL-safe backup/restore of the paper runtime SQLite files (stdlib only).

Backup uses SQLite's online backup API (the same mechanism as the sqlite3 CLI ``.backup``),
so it is consistent while the runtime is writing in WAL mode; copying ``*.sqlite3`` with
``cp`` is NOT safe because committed pages may still live only in ``-wal``.

    python deploy/autonomous/sqlite_backup.py backup  data/autonomous/runtime backups/2026-10-17T0610Z
    python deploy/autonomous/sqlite_backup.py verify  backups/2026-10-17T0610Z
    python deploy/autonomous/sqlite_backup.py restore backups/2026-10-17T0610Z data/autonomous/runtime

``restore`` refuses unless the target has a STOP file and no fresh status.json (runtime
stopped), and it removes stale ``-wal``/``-shm`` files so they cannot be replayed onto the
restored database. Each backup gets a ``MANIFEST.json`` with sizes, sha256 and integrity results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DB_GLOB = "*.sqlite3"
COPY_ALONGSIDE = ("status.json", "jev_schema.approved.json", "jev_schema.proposed.json", "KILL")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _integrity(path: Path) -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def backup(src_dir: Path, dst_dir: Path) -> dict:
    dbs = sorted(src_dir.glob(DB_GLOB))
    if not dbs:
        raise SystemExit(f"no {DB_GLOB} files in {src_dir}")
    dst_dir.mkdir(parents=True, exist_ok=False)
    files = {}
    for db in dbs:
        out = dst_dir / db.name
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # self-contained file, no -wal needed
        finally:
            dst.close()
            src.close()
        files[db.name] = {"bytes": out.stat().st_size, "sha256": _sha256(out), "integrity": _integrity(out)}
    for name in COPY_ALONGSIDE:
        if (src_dir / name).exists():
            shutil.copy2(src_dir / name, dst_dir / name)
            files[name] = {"bytes": (dst_dir / name).stat().st_size, "sha256": _sha256(dst_dir / name)}
    manifest = {"source": str(src_dir), "created_unix": int(time.time()), "files": files}
    (dst_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    bad = {k: v["integrity"] for k, v in files.items() if v.get("integrity", "ok") != "ok"}
    if bad:
        raise SystemExit(f"integrity_check failed: {bad}")
    return manifest


def verify(backup_dir: Path) -> dict:
    manifest = json.loads((backup_dir / "MANIFEST.json").read_text())
    problems = {}
    for name, meta in manifest["files"].items():
        path = backup_dir / name
        if not path.exists() or _sha256(path) != meta["sha256"]:
            problems[name] = "missing or sha256 mismatch"
        elif name.endswith(".sqlite3") and _integrity(path) != "ok":
            problems[name] = "integrity_check failed"
    if problems:
        raise SystemExit(f"backup verification failed: {problems}")
    return manifest


def restore(backup_dir: Path, target_dir: Path, *, max_status_age_s: float = 180.0) -> dict:
    manifest = verify(backup_dir)
    if not (target_dir / "STOP").exists():
        raise SystemExit(f"refusing restore: create {target_dir / 'STOP'} and wait for the runtime to exit first")
    status = target_dir / "status.json"
    if status.exists() and time.time() - status.stat().st_mtime < max_status_age_s:
        raise SystemExit("refusing restore: status.json is fresh, the runtime may still be running")
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in manifest["files"]:
        if name == "KILL":
            continue  # never silently re-arm or clear a kill: the operator decides
        if name.endswith(".sqlite3"):
            for suffix in ("-wal", "-shm"):
                stale = target_dir / f"{name}{suffix}"
                if stale.exists():
                    stale.unlink()
        shutil.copy2(backup_dir / name, target_dir / name)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup"); b.add_argument("src"); b.add_argument("dst")
    v = sub.add_parser("verify"); v.add_argument("backup")
    r = sub.add_parser("restore"); r.add_argument("backup"); r.add_argument("target")
    args = parser.parse_args(argv)
    if args.cmd == "backup":
        result = backup(Path(args.src), Path(args.dst))
    elif args.cmd == "verify":
        result = verify(Path(args.backup))
    else:
        result = restore(Path(args.backup), Path(args.target))
    json.dump({"ok": True, "cmd": args.cmd, "files": sorted(result["files"])}, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
