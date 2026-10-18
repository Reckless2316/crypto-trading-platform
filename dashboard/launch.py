"""Dashboard authority launcher (Task 17): `python -m dashboard.launch --runtime-dir ... --session-file ...`.

Builds the AuthorityGateway from trusted local configuration:
  * control socket `<runtime-dir>/control/control.sock` (hosted by the ledger-mode runtime);
  * one operator session minted by `python -m bot.autonomous.control issue-session`, loaded from a
    0600 file owned by this uid (symlinks, loose modes, readers and expired sessions are refused);
  * the durable 0600 nonce store under `--state-dir`;
  * a one-time bootstrap token printed once to stdout as `http://127.0.0.1:<port>/#bootstrap=<token>`
    (the fragment never reaches server logs; the page exchanges it for the HttpOnly session cookie).
Uvicorn binds 127.0.0.1 only. The control epoch is read with one authenticated status request at start.

HANDOFF(claude-opus-5.5 2026-10-18): (1) control.py rejects a status request whose expected epoch is stale
(STALE_EPOCH) and echoes the caller's epoch, so the current epoch cannot be discovered without guessing; the
launcher probes `--epoch` (default 0, a fresh account) once and reports EPOCH_UNKNOWN otherwise. Needs a
control.py change (epoch-free status, or STALE_EPOCH replies carrying the current permit_epoch). (2) Issued
sessions live at most one hour; the launcher does not hot-reload a re-issued session, so the operator restarts
it. (3) If the operator CLI and the dashboard share one issued session, nonces used by the CLI after launch
collide (reported as outcome unknown); issue a dedicated dashboard session.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from bot.autonomous.control import ControlRejected, ControlSession, PeerCredentials, UnixControlClient
from dashboard.backend.authority_client import (AuthorityGateway, BrowserSession, DurableNonceStore,
                                                bootstrap_token_hash)

LOOPBACK_HOST = "127.0.0.1"
_MAX_SESSION_BYTES = 65_536


class LaunchRefused(RuntimeError):
    """The launcher will not start with this configuration. Messages are stable codes, never secrets."""


@dataclass(frozen=True)
class LaunchConfig:
    runtime_dir: Path
    session_file: Path | None = None
    session_dir: Path | None = None
    state_dir: Path | None = None
    host: str = LOOPBACK_HOST
    port: int = 8000
    epoch: int = 0


@dataclass
class Launch:
    gateway: AuthorityGateway
    bootstrap_token: str
    bootstrap_url: str
    epoch: int | None
    status: dict | None
    notes: list[str] = field(default_factory=list)


def _read_private_json(path: Path) -> object:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise LaunchRefused("SESSION_FILE_MISSING") from None
    except OSError:
        raise LaunchRefused("SESSION_FILE_UNSAFE") from None  # e.g. ELOOP for a symlink
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise LaunchRefused("SESSION_FILE_PERMISSIONS")
        raw = handle.read(_MAX_SESSION_BYTES + 1)
    if len(raw) > _MAX_SESSION_BYTES:
        raise LaunchRefused("SESSION_FILE_INVALID")
    try:
        return json.loads(raw)
    except ValueError:
        raise LaunchRefused("SESSION_FILE_INVALID") from None


def load_session_file(path: Path) -> ControlSession:
    """One issued operator session (uid-bound). Nonces already spent by the operator CLI are excluded."""
    path = Path(path)
    raw = _read_private_json(path)
    try:
        if raw["role"] != "operator":
            raise LaunchRefused("OPERATOR_SESSION_REQUIRED")
        expires = raw["expires_mono_ns"]
        if type(expires) is not int or expires <= time.monotonic_ns():
            raise LaunchRefused("SESSION_EXPIRED")
        used_path = path.parent / (path.name + ".used")
        used = set(_read_private_json(used_path)) if used_path.exists() else set()
        nonces = {n: expires for n in raw["nonces"] if n not in used}
        if not nonces:
            raise LaunchRefused("SESSION_NONCES_EXHAUSTED")
        return ControlSession(raw["session_id"], raw["actor"], raw["role"], raw["account_id"], raw["mode"],
                              raw["boot_id"], raw["token"], PeerCredentials(0, raw["uid"], raw["gid"]), expires,
                              nonces, peer_match="uid")
    except LaunchRefused:
        raise
    except (KeyError, TypeError, ControlRejected):
        raise LaunchRefused("SESSION_FILE_INVALID") from None


def load_session_dir(directory: Path) -> ControlSession:
    """The newest valid operator session in an issue-session directory."""
    candidates = sorted(Path(directory).glob("*.session.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in candidates:
        try:
            return load_session_file(path)
        except LaunchRefused:
            continue
    raise LaunchRefused("NO_VALID_OPERATOR_SESSION")


def build(config: LaunchConfig, *, client=None, monotonic_ns=time.monotonic_ns) -> Launch:
    if config.host != LOOPBACK_HOST:
        raise LaunchRefused("LOOPBACK_ONLY")
    if type(config.port) is not int or not 0 < config.port < 65_536:
        raise LaunchRefused("INVALID_PORT")
    if (config.session_file is None) == (config.session_dir is None):
        raise LaunchRefused("SESSION_SOURCE_REQUIRED")
    control = load_session_file(config.session_file) if config.session_file else load_session_dir(config.session_dir)
    runtime_dir = Path(config.runtime_dir)
    state_dir = Path(config.state_dir) if config.state_dir else runtime_dir.parent / "dashboard-state"
    origin = f"http://{LOOPBACK_HOST}:{config.port}"
    browser = BrowserSession(secrets.token_urlsafe(32), control.actor, control.role, origin, secrets.token_urlsafe(32),
                             control.expires_mono_ns, control)
    token = secrets.token_urlsafe(32)
    gateway = AuthorityGateway(client=client or UnixControlClient(runtime_dir / "control" / "control.sock"),
                               sessions=[browser], nonce_store=DurableNonceStore(state_dir / "nonces.json"),
                               bootstrap_tokens={bootstrap_token_hash(token): browser.session_id},
                               monotonic_ns=monotonic_ns)
    status, note = gateway.probe_status(config.epoch)
    notes = [note] if note else []
    return Launch(gateway, token, f"{origin}/#bootstrap={token}", gateway.control_epoch, status, notes)


def install(app, launch: Launch) -> None:
    app.state.authority_gateway = launch.gateway
    app.state.allowed_hosts = frozenset({LOOPBACK_HOST})


def serve(app, host: str, port: int) -> None:  # pragma: no cover - exercised only by the real launcher
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dashboard.launch",
                                     description="Serve the dashboard on 127.0.0.1 with authenticated authority forwarding.")
    parser.add_argument("--runtime-dir", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session-file", type=Path, help="issued operator session file (0600)")
    source.add_argument("--session-dir", type=Path, help="issue-session directory; the newest valid operator session")
    parser.add_argument("--state-dir", type=Path, default=None, help="durable nonce store directory (0700)")
    parser.add_argument("--host", default=LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--epoch", type=int, default=0, help="expected control epoch for the start-up status probe")
    args = parser.parse_args(argv)
    try:
        result = build(LaunchConfig(args.runtime_dir, args.session_file, args.session_dir, args.state_dir,
                                    args.host, args.port, args.epoch))
    except LaunchRefused as exc:
        parser.exit(2, f"refused: {exc}\n")
    from dashboard.backend.app import app
    install(app, result)
    print(json.dumps({"listen": f"http://{LOOPBACK_HOST}:{args.port}", "control_epoch": result.epoch,
                      "notes": result.notes}), file=sys.stderr, flush=True)
    # The one-time bootstrap URL goes to stdout exactly once; it is never logged.
    print(f"Open once to sign in: {result.bootstrap_url}", flush=True)
    serve(app, LOOPBACK_HOST, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
