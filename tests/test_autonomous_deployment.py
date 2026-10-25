"""Static Task 20 deployment contract checks for the autonomous PAPER runtime.

No Docker is required: compose and workflow files are parsed as YAML, systemd units as text.
`docker compose config` is exercised only when a working Compose CLI is present (skipped otherwise).
"""
from __future__ import annotations

import configparser
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is pinned in requirements-autonomous.lock
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "deploy" / "docker-compose.autonomous.yml"
BASE_COMPOSE = ROOT / "deploy" / "docker-compose.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "autonomous.yml"
RUNBOOK = ROOT / "docs" / "runbooks" / "autonomous-paper.md"
LOCK = ROOT / "requirements-autonomous.lock"
UNITS = ROOT / "deploy" / "autonomous"
GUARD_DIR = UNITS / "ci"
BACKUP_TOOL = UNITS / "sqlite_backup.py"

RUNTIME = "surge-paper-runtime"
RUNTIME_VOLUME = "autonomous-runtime"
ROLES = {"authority": RUNTIME, "reflex": "surge-reflex", "brain": "surge-brain", "dashboard": "surge-dashboard"}
SECRET_OWNERS = {"jev_api_key": "surge-reflex", "anthropic_api_key": "surge-brain"}
PROPOSALS_VOLUME = "autonomous-brain"
DASHBOARD_STATE_VOLUME = "dashboard-state"
SECRETS_DIR = "./deploy/autonomous/secrets/"
SECRET_NAME = re.compile(
    r"(KEY|SECRET|TOKEN|PASSW|PASSPHRASE|PRIVATE|CREDENTIAL|AUTH|COINBASE|BINANCE|KRAKEN|JEV|ANTHROPIC|OPENAI|LIVE)",
    re.I)
REQUIRED_UNSET = {
    "COINBASE_API_KEY", "COINBASE_API_SECRET", "SURGE_COINBASE_API_KEY", "SURGE_COINBASE_API_SECRET",
    "SURGE_BINANCE_API_KEY", "SURGE_BINANCE_API_SECRET", "JEV_API_KEY", "ANTHROPIC_API_KEY",
    "SURGE_LIVE_TRADING_ENABLED", "SURGE_LIVE_TRADING_ARMED",
}


def _yaml(path: Path):
    if yaml is None:
        raise unittest.SkipTest("PyYAML not installed")
    return yaml.safe_load(path.read_text())


def _env_names(service: dict) -> set[str]:
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return set(env)
    return {item.split("=", 1)[0] for item in env}


def _volumes(service: dict) -> list[dict]:
    out = []
    for item in service.get("volumes") or []:
        if isinstance(item, str):
            parts = item.split(":")
            out.append({"source": parts[0], "target": parts[1] if len(parts) > 1 else parts[0],
                        "read_only": len(parts) > 2 and "ro" in parts[2].split(",")})
        else:
            out.append({"source": item.get("source"), "target": item.get("target"),
                        "read_only": bool(item.get("read_only"))})
    return out


def _command_text(service: dict) -> str:
    parts = []
    for key in ("entrypoint", "command"):
        value = service.get(key)
        parts.extend(value if isinstance(value, list) else [value or ""])
    return " ".join(str(p) for p in parts)


def _unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


class OverlayRuntimeServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = _yaml(OVERLAY)
        self.runtime = self.config["services"][RUNTIME]

    def test_runtime_environment_has_no_secret_names(self):
        names = _env_names(self.runtime)
        leaked = sorted(n for n in names if SECRET_NAME.search(n))
        self.assertEqual(leaked, [], f"secret-like env names in {RUNTIME}: {leaked}")
        self.assertNotIn("env_file", self.runtime)
        self.assertNotIn("secrets", self.runtime)

    def test_overlay_never_introduces_secret_env_or_env_files_anywhere(self):
        for name, service in self.config["services"].items():
            with self.subTest(service=name):
                self.assertNotIn("env_file", service)
                self.assertEqual(sorted(n for n in _env_names(service) if SECRET_NAME.search(n)), [])
                for value in (service.get("environment") or {}).values() if isinstance(
                        service.get("environment"), dict) else []:
                    self.assertNotRegex(str(value), r"(sk-|KEY|SECRET|TOKEN)")
        self.assertNotRegex(OVERLAY.read_text(), r"\$\{?[A-Z_]*(KEY|SECRET|TOKEN)")

    def test_runtime_is_nonroot_readonly_and_unprivileged(self):
        self.assertIs(self.runtime.get("read_only"), True)
        user = str(self.runtime.get("user", ""))
        self.assertTrue(user, "user must be explicit")
        self.assertNotIn(user.split(":")[0], {"root", "0"})
        self.assertIn("ALL", self.runtime.get("cap_drop", []))
        self.assertNotIn("cap_add", self.runtime)
        self.assertIn("no-new-privileges:true", self.runtime.get("security_opt", []))
        self.assertFalse(self.runtime.get("privileged", False))
        self.assertNotIn(self.runtime.get("network_mode"), ("host",))
        self.assertNotIn("pid", self.runtime)
        self.assertNotIn("ipc", self.runtime)
        self.assertNotIn("devices", self.runtime)
        self.assertEqual(self.runtime.get("pull_policy"), "never")

    def test_runtime_mounts_are_restricted_to_one_writable_runtime_volume(self):
        volumes = _volumes(self.runtime)
        self.assertEqual(len(volumes), 1, volumes)
        (mount,) = volumes
        self.assertEqual(mount["source"], RUNTIME_VOLUME)
        self.assertFalse(mount["source"].startswith(("/", ".", "~")), "no host bind mounts")
        self.assertNotIn("docker.sock", mount["target"])
        self.assertIn(RUNTIME_VOLUME, self.config.get("volumes", {}))
        tmpfs = self.runtime.get("tmpfs", [])
        self.assertTrue(all(t.split(":")[0] == "/tmp" for t in tmpfs), tmpfs)
        self.assertNotIn("volumes_from", self.runtime)

    def test_runtime_data_dir_and_healthcheck_live_on_the_writable_mount(self):
        target = _volumes(self.runtime)[0]["target"].rstrip("/")
        command = _command_text(self.runtime)
        match = re.search(r"--data-dir\s+(\S+)", command)
        self.assertIsNotNone(match, command)
        self.assertTrue(match.group(1).startswith(target + "/"), (match.group(1), target))
        health = " ".join(self.runtime["healthcheck"]["test"])
        self.assertIn(f"{match.group(1)}/status.json", health)
        self.assertIn("at_us", health, "healthcheck must check freshness, not mere existence")

    def test_runtime_publishes_no_ports_and_is_isolated_from_dashboard_network(self):
        self.assertNotIn("ports", self.runtime)
        self.assertNotIn("expose", self.runtime)
        dashboard = self.config["services"][ROLES["dashboard"]]
        self.assertEqual("host", dashboard.get("network_mode"))  # loopback-only bind on the host
        self.assertNotIn("networks", dashboard)
        self.assertTrue(self.runtime.get("networks"))

    def test_runtime_command_is_paper_only_and_unarmed(self):
        command = _command_text(self.runtime)
        self.assertIn("-m bot.autonomous.runtime --paper", command)
        for forbidden in ("--arm-paper", "--reflex jev", "--live", "SURGE_LIVE"):
            self.assertNotIn(forbidden, command)
        self.assertEqual(self.runtime.get("restart"), "on-failure", "a STOP-file exit must stay stopped")

    def test_dashboard_gets_runtime_volume_read_only_at_the_dir_it_reads(self):
        surge = self.config["services"][ROLES["dashboard"]]
        mounts = [m for m in _volumes(surge) if m["source"] == RUNTIME_VOLUME]
        self.assertEqual(len(mounts), 1)
        self.assertTrue(mounts[0]["read_only"])
        runtime_dir = surge["environment"]["AUTONOMOUS_RUNTIME_DIR"]
        self.assertTrue(runtime_dir.startswith(mounts[0]["target"].rstrip("/") + "/"))
        runtime_subdir = re.search(r"--data-dir\s+(\S+)", _command_text(self.runtime)).group(1)
        runtime_target = _volumes(self.runtime)[0]["target"].rstrip("/")
        self.assertEqual(runtime_dir[len(mounts[0]["target"].rstrip("/")):],
                         runtime_subdir[len(runtime_target):], "dashboard must read the same subdirectory")

    def test_overlay_targets_the_existing_base_service(self):
        base = _yaml(BASE_COMPOSE)
        self.assertIn("surge", base["services"])
        self.assertEqual(self.runtime["image"], base["services"]["surge"]["image"])

    def test_docker_compose_config_resolves_when_available(self):
        try:
            probe = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("docker CLI unavailable")
        if probe.returncode != 0:
            self.skipTest("docker compose plugin unavailable")
        result = subprocess.run(
            ["docker", "compose", "-f", str(BASE_COMPOSE), "-f", str(OVERLAY), "--project-directory", str(ROOT),
             "config", "--format", "json"], capture_output=True, text=True, timeout=60,
            env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(result.returncode, 0, result.stderr)
        resolved = json.loads(result.stdout)["services"][RUNTIME]
        self.assertNotIn("ports", resolved)
        self.assertEqual(sorted(n for n in resolved.get("environment", {}) if SECRET_NAME.search(n)), [])


class FourRoleSplitTests(unittest.TestCase):
    """authority / reflex / brain / dashboard: role-specific file secrets and minimal mounts."""

    def setUp(self):
        self.config = _yaml(OVERLAY)
        self.services = self.config["services"]

    def service(self, role):
        return self.services[ROLES[role]]

    def test_roles_exist_and_optional_roles_are_profile_gated(self):
        for role in ROLES:
            self.assertIn(ROLES[role], self.services, role)
        self.assertNotIn("profiles", self.service("authority"))
        self.assertNotIn("profiles", self.service("dashboard"))
        self.assertEqual(["jev"], self.service("reflex")["profiles"])
        self.assertEqual(["brain"], self.service("brain")["profiles"])
        self.assertEqual(["legacy-display"], self.services["surge"]["profiles"],
                         "the legacy env-configured dashboard must not start in the role deployment")

    def test_every_secret_is_file_backed_and_mounted_into_exactly_one_role(self):
        declared = self.config.get("secrets") or {}
        self.assertEqual(set(SECRET_OWNERS), set(declared))
        for name, spec in declared.items():
            with self.subTest(secret=name):
                self.assertEqual({"file"}, set(spec), "file-mounted only; never environment-sourced")
                self.assertTrue(spec["file"].startswith(SECRETS_DIR), spec["file"])
                owners = [svc for svc, body in self.services.items()
                          if name in [s if isinstance(s, str) else s.get("source") for s in body.get("secrets") or []]]
                self.assertEqual([SECRET_OWNERS[name]], owners)
        for role in ("authority", "dashboard"):
            self.assertNotIn("secrets", self.service(role), role)
        for role, expected in (("reflex", ["jev_api_key"]), ("brain", ["anthropic_api_key"])):
            mounted = [s if isinstance(s, str) else s.get("source") for s in self.service(role)["secrets"]]
            self.assertEqual(expected, mounted, role)
        ignore = ROOT / "deploy" / "autonomous" / "secrets" / ".gitignore"
        self.assertTrue(ignore.exists())
        self.assertIn("*", ignore.read_text().split())

    def test_mounts_are_minimal_per_role(self):
        def mounts(role):
            return sorted((m["source"], m["read_only"]) for m in _volumes(self.service(role)))
        self.assertEqual([(RUNTIME_VOLUME, False)], mounts("authority"))
        self.assertEqual([], mounts("reflex"))
        self.assertEqual(sorted([(RUNTIME_VOLUME, True), (PROPOSALS_VOLUME, False)]), mounts("brain"))
        self.assertEqual(sorted([(RUNTIME_VOLUME, True), (PROPOSALS_VOLUME, True), (DASHBOARD_STATE_VOLUME, False)]),
                         mounts("dashboard"))
        for role in ROLES:
            for m in _volumes(self.service(role)):
                self.assertFalse(str(m["source"]).startswith(("/", ".", "~")), "no host bind mounts")
                self.assertIn(m["source"], self.config["volumes"])
            self.assertNotIn("volumes_from", self.service(role))

    def test_every_role_is_hardened(self):
        for role in ROLES:
            body = self.service(role)
            with self.subTest(role=role):
                self.assertIs(True, body.get("read_only"))
                self.assertEqual("surge", str(body.get("user")))
                self.assertIn("ALL", body.get("cap_drop", []))
                self.assertIn("no-new-privileges:true", body.get("security_opt", []))
                self.assertFalse(body.get("privileged", False))
                self.assertEqual("never", body.get("pull_policy"))
                self.assertNotIn("ports", body)
                self.assertNotIn("cap_add", body)

    def test_networks_are_role_specific(self):
        nets = {role: set(self.service(role).get("networks") or []) for role in ("authority", "reflex", "brain")}
        for role, value in nets.items():
            self.assertEqual(1, len(value), role)
        self.assertEqual(3, len(set().union(*nets.values())), "no shared egress network between roles")
        hosts = [svc for svc, body in self.services.items() if body.get("network_mode") == "host"]
        self.assertEqual([ROLES["dashboard"]], hosts)

    def test_role_commands(self):
        authority = _command_text(self.service("authority"))
        self.assertIn("--accounting ledger", authority)
        brain = self.service("brain")
        text = _command_text(brain)
        self.assertIn("-m bot.autonomous.nightly", text)
        self.assertIn("--brain anthropic", text)
        self.assertIn("--brain-key-file /run/secrets/anthropic_api_key", text)
        rw = [m["target"] for m in _volumes(brain) if not m["read_only"]][0].rstrip("/")
        ro = [m["target"] for m in _volumes(brain) if m["read_only"]][0].rstrip("/")
        self.assertTrue(re.search(r"--out-dir\s+(\S+)", text).group(1).startswith(rw + "/"))
        self.assertTrue(re.search(r"--data-dir\s+(\S+)", text).group(1).startswith(ro + "/"))
        dashboard = self.service("dashboard")
        text = _command_text(dashboard)
        self.assertIn("-m dashboard.launch", text)
        self.assertIn("--host 127.0.0.1", text)
        runtime_dir = re.search(r"--runtime-dir\s+(\S+)", text).group(1)
        self.assertEqual(dashboard["environment"]["AUTONOMOUS_RUNTIME_DIR"], runtime_dir)
        self.assertTrue(re.search(r"--session-dir\s+(\S+)", text).group(1).startswith(runtime_dir + "/control/sessions"))
        state = [m["target"] for m in _volumes(dashboard) if not m["read_only"]][0].rstrip("/")
        self.assertTrue(re.search(r"--state-dir\s+(\S+)", text).group(1).startswith(state + "/"))
        brain_out = re.search(r"--out-dir\s+(\S+)", _command_text(brain)).group(1)
        brain_rw = [m["target"] for m in _volumes(brain) if not m["read_only"]][0].rstrip("/")
        dash_ro = [m["target"] for m in _volumes(dashboard) if m["source"] == PROPOSALS_VOLUME][0].rstrip("/")
        self.assertEqual(dash_ro + brain_out[len(brain_rw):] + "/proposals", dashboard["environment"]["AUTONOMOUS_PROPOSALS_DIR"])
        reflex = _command_text(self.service("reflex"))
        self.assertIn("HANDOFF", reflex, "reflex cannot run as a separate process yet; it must refuse")
        self.assertNotIn("bot.autonomous.runtime", reflex)


class SystemdUnitTests(unittest.TestCase):
    def test_units_unset_secrets_and_stay_paper(self):
        for name in ("surge-paper-runtime.service", "surge-nightly.service"):
            with self.subTest(unit=name):
                service = _unit(UNITS / name)["Service"]
                unset = set(service.get("UnsetEnvironment", "").split())
                self.assertTrue(REQUIRED_UNSET <= unset, sorted(REQUIRED_UNSET - unset))
                self.assertEqual(service.get("NoNewPrivileges"), "true")
                self.assertNotIn("Environment", service)
                self.assertNotIn("EnvironmentFile", service)
                exec_start = service["ExecStart"]
                self.assertIn("-m bot.autonomous.runtime --paper", exec_start)
                for forbidden in ("--arm-paper", "--reflex jev", "--live"):
                    self.assertNotIn(forbidden, exec_start)
        runtime = _unit(UNITS / "surge-paper-runtime.service")["Service"]
        self.assertEqual(runtime.get("Restart"), "on-failure")
        self.assertIn("STOP", runtime.get("ExecStartPre", ""))
        self.assertNotIn("KILL", runtime.get("ExecStartPre", ""), "start must never clear a KILL")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = _yaml(WORKFLOW)
        self.job = self.workflow["jobs"]["test"]
        self.steps = self.job["steps"]

    def _step(self, needle: str) -> dict:
        matches = [s for s in self.steps if needle in str(s.get("run", ""))]
        self.assertEqual(len(matches), 1, f"expected one step running {needle!r}")
        return matches[0]

    def test_python_312_and_locked_install(self):
        setup = [s for s in self.steps if str(s.get("uses", "")).startswith("actions/setup-python@")]
        self.assertEqual(len(setup), 1)
        self.assertEqual(str(setup[0]["with"]["python-version"]), "3.12")
        install = self._step("pip install --no-deps -r requirements-autonomous.lock")
        self.assertIn("pip check", install["run"])

    def test_workflow_runs_the_full_suite_with_network_blocked(self):
        suite = self._step("python -m unittest discover -s tests")
        env = {**self.job.get("env", {}), **suite.get("env", {})}
        self.assertEqual(str(env["SURGE_BLOCK_NETWORK"]), "1")
        self.assertEqual(env["PYTHONPATH"], "deploy/autonomous/ci")
        self.assertTrue((ROOT / env["PYTHONPATH"] / "sitecustomize.py").exists())
        check = self._step("SURGE_NETWORK_VIOLATIONS_LOG")
        self.assertEqual(check.get("if"), "always()")
        self.assertIn("exit 1", check["run"])
        self.assertLess(self.steps.index(suite), self.steps.index(check))

    def test_workflow_is_read_only_and_secret_free(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        text = WORKFLOW.read_text()
        self.assertNotIn("secrets.", text)
        for verb in ("docker push", "docker compose up", "deploy.sh", "git push"):
            self.assertNotIn(verb, text)
        for step in self.steps:
            uses = step.get("uses")
            if uses:
                self.assertRegex(uses, r"@[0-9a-f]{40}$", f"action not pinned to a commit: {uses}")


class LockFileTests(unittest.TestCase):
    def test_lock_pins_every_package_exactly(self):
        lines = [l.strip() for l in LOCK.read_text().splitlines() if l.strip() and not l.startswith("#")]
        self.assertTrue(lines)
        for line in lines:
            # Exact pin, optionally followed by a PEP 508 marker (coincurve applies only below 3.14).
            self.assertRegex(line, r"^[A-Za-z0-9_.\-\[\]]+==[0-9][^ ;]*( ; [a-z_]+ *[<>=!]=? *\"[^\"]+\")?$", line)
        pinned = {re.split(r"[\[=]", l, maxsplit=1)[0].lower().replace("_", "-") for l in lines}
        for req in (ROOT / "requirements.txt").read_text().splitlines():
            req = req.strip()
            if req and not req.startswith(("#", "-")):
                name = re.split(r"[\[<>=!~ ]", req, maxsplit=1)[0].lower().replace("_", "-")
                self.assertIn(name, pinned, f"{name} from requirements.txt missing in lock")
        self.assertIn("pyyaml", pinned, "deployment tests parse YAML")


class NetworkGuardTests(unittest.TestCase):
    PROBE = r"""
import socket, sys
blocked = []
for fn in (lambda: socket.create_connection(("192.0.2.1", 443), timeout=1),
           lambda: socket.getaddrinfo("example.com", 443),
           lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("198.51.100.7", 80))):
    try:
        fn()
    except OSError as exc:
        blocked.append(type(exc).__name__)
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
cli = socket.create_connection(srv.getsockname(), timeout=2); cli.close(); srv.close()
print(",".join(blocked))
"""

    def test_guard_blocks_and_logs_external_calls_but_allows_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "violations.jsonl"
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(GUARD_DIR),
                   "SURGE_BLOCK_NETWORK": "1", "SURGE_NETWORK_VIOLATIONS_LOG": str(log)}
            result = subprocess.run([sys.executable, "-c", self.PROBE], capture_output=True, text=True,
                                    timeout=30, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "NetworkBlockedError,NetworkBlockedError,NetworkBlockedError")
            kinds = [json.loads(line)["kind"] for line in log.read_text().splitlines()]
            self.assertEqual(kinds, ["create_connection", "getaddrinfo", "connect"])

    def test_guard_is_inert_without_the_flag(self):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(GUARD_DIR)}
        result = subprocess.run([sys.executable, "-c", "import socket; print(socket.getaddrinfo.__module__)"],
                                capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), "sitecustomize")


class SqliteBackupTests(unittest.TestCase):
    def _run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BACKUP_TOOL), *map(str, args)], capture_output=True, text=True,
                              timeout=60)

    def test_backup_captures_committed_wal_frames_and_restore_is_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, backup_dir = Path(tmp) / "runtime", Path(tmp) / "backup"
            live.mkdir()
            writer = sqlite3.connect(live / "runtime.sqlite3", isolation_level=None)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")  # keep committed rows only in -wal
            writer.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY, v TEXT)")
            writer.executemany("INSERT INTO fills(v) VALUES (?)", [(str(i),) for i in range(50)])
            self.assertGreater((live / "runtime.sqlite3-wal").stat().st_size, 0)
            (live / "KILL").touch()
            try:
                result = self._run("backup", live, backup_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                copy = sqlite3.connect(backup_dir / "runtime.sqlite3")
                self.assertEqual(copy.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 50)
                copy.close()
                self.assertEqual(self._run("verify", backup_dir).returncode, 0)

                writer.execute("INSERT INTO fills(v) VALUES ('after-backup')")
                (live / "status.json").write_text("{}")
                refused = self._run("restore", backup_dir, live)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("STOP", refused.stderr)
            finally:
                writer.close()

            (live / "STOP").touch()
            (live / "KILL").unlink()
            old = time.time() - 3600
            os.utime(live / "status.json", (old, old))
            (live / "runtime.sqlite3-wal").write_bytes(b"stale")
            result = self._run("restore", backup_dir, live)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((live / "runtime.sqlite3-wal").exists())
            self.assertFalse((live / "KILL").exists(), "restore must not recreate KILL")
            restored = sqlite3.connect(live / "runtime.sqlite3")
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 50)
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            restored.close()

    def test_verify_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, backup_dir = Path(tmp) / "runtime", Path(tmp) / "backup"
            live.mkdir()
            con = sqlite3.connect(live / "runtime.sqlite3")
            con.execute("CREATE TABLE t(x)"); con.commit(); con.close()
            self.assertEqual(self._run("backup", live, backup_dir).returncode, 0)
            with (backup_dir / "runtime.sqlite3").open("ab") as fh:
                fh.write(b"tamper")
            self.assertNotEqual(self._run("verify", backup_dir).returncode, 0)


class RunbookTests(unittest.TestCase):
    def test_runbook_covers_required_operations(self):
        text = RUNBOOK.read_text()
        for needle in ("STOP", "KILL", "Restart and recovery", ".backup", "WAL", "±10 s", "4 s",
                       "status.json", "--report", "--write-jev-schema", "jev_schema.approved.json",
                       "Live trading is not available", "Task", "approval", "issue-session", "dashboard.launch",
                       "#bootstrap=", "--profile brain", "--profile jev", "deploy/autonomous/secrets",
                       "Four-role deployment"):
            self.assertIn(needle, text, needle)


if __name__ == "__main__":
    unittest.main()
