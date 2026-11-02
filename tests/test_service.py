"""`pengumon service install | uninstall | status`: the definitions written for systemd, launchd and Task Scheduler,
the steps that carry them out (with a fake command runner: nothing here touches the real service manager), and the
refusals. Everything runs on every OS; a test that needs a Linux tool skips without it."""

import plistlib
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pengumon import cli, service
from pengumon.service import SYSTEM, USER

ROOT = Path(__file__).resolve().parents[1]
EXE = "/opt/pengumon/venv/bin/pengumon"


def _unit(text: str) -> dict:
    """A systemd unit as {section: {key: [values]}} (keys repeat, so configparser's single value would hide lines)."""
    out, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = out.setdefault(line.strip("[]"), {})
        else:
            key, _, value = line.partition("=")
            section.setdefault(key, []).append(value)
    return out


class FakeRun:
    """Records the commands; `answers` maps the start of a command (a tuple) to (returncode, stdout)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        for prefix, (code, out) in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(list(argv), code, out, "boom" if code else "")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


# ---- the Linux system unit --------------------------------------------------------------------------------------------

def test_system_unit_runs_as_a_throwaway_user_with_one_capability():
    service_section = _unit(service.render_systemd_system(EXE))["Service"]
    assert service_section["ExecStart"] == [f"{EXE} run"]
    assert service_section["DynamicUser"] == ["yes"]
    assert service_section["StateDirectory"] == ["pengumon"]
    assert service_section["CapabilityBoundingSet"] == ["CAP_NET_RAW"]
    assert service_section["AmbientCapabilities"] == ["CAP_NET_RAW"]
    assert service_section["NoNewPrivileges"] == ["yes"]
    assert service_section["ProtectSystem"] == ["strict"]
    assert service_section["ProtectHome"] == ["yes"]
    assert service_section["UMask"] == ["0077"]
    assert service_section["Restart"] == ["on-failure"]
    assert "PENGUMON_DATA=/var/lib/pengumon" in service_section["Environment"]


def test_system_unit_allows_the_address_families_the_app_needs():
    families = _unit(service.render_systemd_system(EXE))["Service"]["RestrictAddressFamilies"][0].split()
    # ICMP (AF_INET), IPv6 probes, the local socket, and the route table the gateway lookup reads (AF_NETLINK)
    assert {"AF_INET", "AF_INET6", "AF_UNIX", "AF_NETLINK"} == set(families)


def test_system_unit_carries_the_host_and_port_it_was_asked_for():
    text = service.render_systemd_system(EXE, {"PENGUMON_HOST": "0.0.0.0", "PENGUMON_PORT": "9000"})
    env = _unit(text)["Service"]["Environment"]
    assert "PENGUMON_HOST=0.0.0.0" in env and "PENGUMON_PORT=9000" in env


def test_user_unit_has_no_sandboxing_that_needs_namespaces():
    section = _unit(service.render_systemd_user(EXE))["Service"]
    assert section["NoNewPrivileges"] == ["yes"]
    for key in ("ProtectSystem", "ProtectHome", "PrivateTmp", "PrivateDevices", "DynamicUser", "StateDirectory"):
        assert key not in section, key
    assert _unit(service.render_systemd_user(EXE))["Install"]["WantedBy"] == ["default.target"]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_system_unit_scores_well_in_systemd_analyze_security(tmp_path):
    """The exposure score the documentation quotes. The binary has to exist for the offline analysis."""
    exe = tmp_path / "pengumon"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    unit = tmp_path / "pengumon.service"
    unit.write_text(service.render_systemd_system(str(exe)))
    done = subprocess.run(["systemd-analyze", "security", "--offline=true", "--no-pager", str(unit)],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0 and "offline" in (done.stderr or ""):
        pytest.skip("this systemd-analyze has no --offline")
    assert done.returncode == 0, done.stderr
    line = next(ln for ln in done.stdout.splitlines() if "Overall exposure level" in ln)
    score = float(line.split(":")[1].split()[0])
    assert score <= service.MAX_EXPOSURE, line


# ---- macOS ---------------------------------------------------------------------------------------------------------------

def test_launchd_daemon_plist_runs_as_the_dedicated_account():
    data = plistlib.loads(service.render_launchd("/usr/local/pengumon/venv/bin/pengumon", {"PENGUMON_DATA": "/d"},
                                                 "/d/pengumon.log", service.MAC_USER).encode())
    assert data["Label"] == service.LABEL
    assert data["ProgramArguments"] == ["/usr/local/pengumon/venv/bin/pengumon", "run"]
    assert data["UserName"] == "_pengumon" and data["GroupName"] == "_pengumon"
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["EnvironmentVariables"]["PENGUMON_DATA"] == "/d"
    assert data["StandardOutPath"] == data["StandardErrorPath"] == "/d/pengumon.log"


def test_launchd_agent_plist_has_no_account():
    data = plistlib.loads(service.render_launchd("/x/pengumon", {}, "/x/log").encode())
    assert "UserName" not in data and "GroupName" not in data


def test_mac_account_is_created_with_a_free_id_and_no_login():
    run = FakeRun({("dscl", ".", "-read"): (1, ""), ("dscl", ".", "-list"): (0, "root 0\n_foo 450\n_bar 451\nme 501\n")})
    service._ensure_mac_user(run)
    creates = [c for c in run.calls if "-create" in c]
    assert ["dscl", ".", "-create", "/Users/_pengumon", "UniqueID", "452"] in creates
    assert ["dscl", ".", "-create", "/Users/_pengumon", "UserShell", "/usr/bin/false"] in creates
    assert ["dscl", ".", "-create", "/Users/_pengumon", "IsHidden", "1"] in creates


def test_mac_account_that_exists_is_left_alone():
    run = FakeRun({("dscl", ".", "-read"): (0, "UniqueID: 452")})
    service._ensure_mac_user(run)
    assert len(run.calls) == 1


# ---- Windows ---------------------------------------------------------------------------------------------------------------

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_task_xml_starts_at_logon_for_the_user_without_admin_rights():
    root = ET.fromstring(service.render_task_xml(r"C:\P\pengumonw.exe", "run", r"HOME\bo", r"C:\P"))
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == r"HOME\bo"
    principal = root.find("t:Principals/t:Principal", NS)
    assert principal.find("t:UserId", NS).text == r"HOME\bo"
    assert principal.find("t:RunLevel", NS).text == "LeastPrivilege"
    assert principal.find("t:LogonType", NS).text == "InteractiveToken"
    settings = root.find("t:Settings", NS)
    assert settings.find("t:ExecutionTimeLimit", NS).text == "PT0S"          # never stopped for running long
    assert settings.find("t:DisallowStartIfOnBatteries", NS).text == "false"
    assert settings.find("t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert settings.find("t:RestartOnFailure/t:Count", NS) is not None
    exe = root.find("t:Actions/t:Exec", NS)
    assert exe.find("t:Command", NS).text == r"C:\P\pengumonw.exe" and exe.find("t:Arguments", NS).text == "run"


def test_task_xml_escapes_names():
    root = ET.fromstring(service.render_task_xml(r"C:\A & B\pengumonw.exe", "run", r"D\o'<x>", r"C:\A & B"))
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == r"D\o'<x>"
    assert root.find("t:Actions/t:Exec/t:Command", NS).text == r"C:\A & B\pengumonw.exe"


def test_windows_account_is_domain_and_user():
    assert service.windows_account({"USERDOMAIN": "PC", "USERNAME": "bo"}) == "PC\\bo"


# ---- the plans ---------------------------------------------------------------------------------------------------------------

def _commands(plan):
    return [s.argv for s in plan.steps if s.kind == "run"]


def test_linux_system_plan_writes_the_unit_then_enables_and_restarts_it():
    plan = service.plan_install("linux", SYSTEM, python="/opt/pengumon/venv/bin/python")
    assert plan.mode == "systemd-system"
    assert plan.steps[0].kind == "write" and plan.steps[0].path == "/etc/systemd/system/pengumon.service"
    assert plan.steps[0].mode == 0o644
    assert _commands(plan) == [["systemctl", "daemon-reload"], ["systemctl", "enable", "pengumon.service"],
                               ["systemctl", "restart", "pengumon.service"]]
    assert f"ExecStart={EXE} run" in plan.steps[0].text


def test_linux_user_plan_uses_the_user_manager_and_the_xdg_config_home(tmp_path):
    plan = service.plan_install("linux", USER, python="/home/a/.local/share/pengumon/venv/bin/python",
                                home=tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "cfg")}, linger=False,
                                account="alex")
    assert plan.steps[0].path == str(tmp_path / "cfg" / "systemd" / "user" / "pengumon.service")
    assert all(c[:2] == ["systemctl", "--user"] for c in _commands(plan))
    assert any("enable-linger alex" in n for n in plan.notes)
    assert not any("linger" in n for n in service.plan_install("linux", USER, linger=True).notes)


def test_macos_daemon_plan_creates_the_account_and_data_directory_before_loading():
    plan = service.plan_install("macos", SYSTEM, python="/usr/local/pengumon/venv/bin/python", port=8123)
    kinds = [s.kind for s in plan.steps]
    assert kinds[:2] == ["mac_user", "mkdir"] and plan.steps[1].owner == "_pengumon" and plan.steps[1].mode == 0o700
    assert kinds.index("write") > kinds.index("mkdir")
    assert ["launchctl", "bootstrap", "system", str(service.MAC_DAEMON_PLIST)] in _commands(plan)
    loaded = plistlib.loads(plan.files[str(service.MAC_DAEMON_PLIST)].encode())
    assert loaded["EnvironmentVariables"]["PENGUMON_DATA"] == "/Library/Application Support/Pengumon"
    assert loaded["EnvironmentVariables"]["PENGUMON_PORT"] == "8123"


def test_macos_agent_plan_lives_in_the_users_library(tmp_path):
    plan = service.plan_install("macos", USER, python="/x/bin/python", home=tmp_path)
    assert plan.mode == "launchd-agent"
    assert any(s.kind == "write" and s.path == str(tmp_path / "Library/LaunchAgents/com.pengupowered.pengumon.plist")
               for s in plan.steps)
    assert any("local network" in n for n in plan.notes)


def test_windows_plan_registers_a_task_from_an_xml_file_and_starts_it():
    plan = service.plan_install("windows", USER, python=r"C:\P\Scripts\python.exe", account=r"PC\bo", port=9001)
    assert plan.mode == "windows-task"
    commands = _commands(plan)
    assert ["schtasks", "/Create", "/TN", "Pengumon", "/XML", "<task.xml>", "/F"] in commands
    assert commands[-1] == ["schtasks", "/Run", "/TN", "Pengumon"]
    xml = plan.files["<task.xml>"]
    assert "pengumonw.exe" in xml and "run --port 9001" in xml and r"PC\bo" in xml


def test_uninstall_plans_stop_before_removing_and_keep_the_data():
    linux = service.plan_uninstall("linux", SYSTEM)
    assert linux.steps[0].argv == ["systemctl", "disable", "--now", "pengumon.service"] and linux.steps[0].tolerate
    assert [s.kind for s in linux.steps] == ["run", "remove", "run"]
    assert any("data is kept" in n for n in linux.notes)
    mac = service.plan_uninstall("macos", SYSTEM)
    assert mac.steps[0].argv[:2] == ["launchctl", "bootout"] and mac.steps[1].kind == "remove"
    win = service.plan_uninstall("windows", USER)
    assert win.steps[-1].argv == ["schtasks", "/Delete", "/TN", "Pengumon", "/F"]
    for plan in (linux, mac, win):
        assert not any(s.kind in ("mkdir", "write") for s in plan.steps)


# ---- carrying out ----------------------------------------------------------------------------------------------------------

def test_run_plan_writes_the_file_with_its_mode_then_runs_the_commands_in_order(tmp_path, capsys):
    target = tmp_path / "sub" / "pengumon.service"
    plan = service.Plan("t", [service.Step("write", str(target), "hello\n", 0o640),
                              service.Step("run", argv=["a"]), service.Step("run", argv=["b"])], ["done"])
    run = FakeRun()
    service.run_plan(plan, run)
    assert target.read_text() == "hello\n"
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o640
    assert run.calls == [["a"], ["b"]]
    assert "done" in capsys.readouterr().out


def test_run_plan_stops_at_the_first_failure_but_tolerates_marked_steps():
    plan = service.Plan("t", [service.Step("run", argv=["a"], tolerate=True), service.Step("run", argv=["b"]),
                              service.Step("run", argv=["c"])])
    run = FakeRun({("a",): (1, ""), ("b",): (5, "")})
    with pytest.raises(service.ServiceError, match=r"b failed \(5\): boom"):
        service.run_plan(plan, run)
    assert run.calls == [["a"], ["b"]]


def test_run_plan_gives_task_scheduler_a_utf16_file_and_removes_it(tmp_path):
    plan = service.Plan("windows-task", [service.Step("write", "<task.xml>", "<Task>ü</Task>", 0o600),
                                         service.Step("run", argv=["schtasks", "/Create", "/XML", "<task.xml>"])])
    seen = {}

    def run(argv):
        path = Path(argv[-1])
        seen["raw"] = path.read_bytes()
        seen["path"] = path
        return subprocess.CompletedProcess(argv, 0, "", "")

    service.run_plan(plan, run)
    assert seen["raw"].startswith(b"\xff\xfe") and seen["raw"][2:].decode("utf-16-le") == "<Task>ü</Task>"
    assert not seen["path"].exists()


def test_a_missing_tool_is_a_failed_step_not_a_traceback():
    done = service.default_runner(["definitely-not-a-program-pengumon"])
    assert done.returncode == 127


# ---- the command line --------------------------------------------------------------------------------------------------------

@pytest.fixture
def as_linux(monkeypatch):
    monkeypatch.setattr(service, "system_kind", lambda platform=None: "linux")


def test_install_dry_run_prints_the_unit_and_changes_nothing(as_linux, capsys, monkeypatch):
    monkeypatch.setattr(service, "run_plan", lambda *a, **k: pytest.fail("a dry run must not run anything"))
    assert cli.main(["service", "install", "--dry-run", "--system", "--port", "9100"]) == 0
    out = capsys.readouterr().out
    assert "Would install Pengumon" in out and "/etc/systemd/system/pengumon.service" in out
    assert "PENGUMON_PORT=9100" in out and "CapabilityBoundingSet=CAP_NET_RAW" in out


def test_install_system_without_root_says_to_use_sudo_or_user(as_linux, monkeypatch, capsys):
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000, raising=False)
    assert cli.main(["service", "install", "--system"]) == 1
    err = capsys.readouterr().err
    assert "needs root" in err and "sudo" in err and "--user" in err


def test_install_user_as_root_is_refused(as_linux, monkeypatch, capsys):
    monkeypatch.setattr(service.os, "geteuid", lambda: 0, raising=False)
    assert cli.main(["service", "install", "--user"]) == 1
    assert "root's own user manager" in capsys.readouterr().err


def test_system_and_user_exclude_each_other(as_linux, capsys):
    assert cli.main(["service", "install", "--system", "--user", "--dry-run"]) == 1
    assert "exclude each other" in capsys.readouterr().err


def test_install_refuses_when_the_console_script_is_not_there(as_linux, monkeypatch, capsys):
    monkeypatch.setattr(service.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(service.sys, "executable", "/nonexistent/venv/bin/python")
    assert cli.main(["service", "install"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_install_as_root_installs_the_system_service(as_linux, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service.os, "geteuid", lambda: 0, raising=False)
    (tmp_path / "pengumon").write_text("#!/bin/sh\n")
    monkeypatch.setattr(service.sys, "executable", str(tmp_path / "python"))
    unit = tmp_path / "etc" / "pengumon.service"
    monkeypatch.setattr(service, "SYSTEM_UNIT_PATH", unit)
    run = FakeRun()
    assert service.cmd_install(cli.build_parser().parse_args(["service", "install"]), run) == 0
    text = unit.read_bytes().decode("utf-8")  # as written: the unit is for Linux, so LF on every host
    assert "\r" not in text
    # a Windows tmp_path has a backslash and a colon, so systemd quoting applies to it; a POSIX one stays bare
    exe = str(tmp_path / "pengumon")
    expected = exe if all(c in service._SD_PLAIN for c in exe) else '"' + exe.replace("\\", "\\\\") + '"'
    assert f"ExecStart={expected} run" in text
    assert [c[1] for c in run.calls if c[0] == "systemctl"] == ["daemon-reload", "enable", "restart"]
    assert "http://127.0.0.1:8000/" in capsys.readouterr().out


def test_uninstall_without_a_service_says_so(as_linux, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service, "SYSTEM_UNIT_PATH", tmp_path / "none.service")
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: tmp_path))
    assert cli.main(["service", "uninstall"]) == 0
    assert "not installed" in capsys.readouterr().out


def test_status_exit_codes(as_linux, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service, "SYSTEM_UNIT_PATH", tmp_path / "pengumon.service")
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: tmp_path))
    args = cli.build_parser().parse_args(["service", "status"])
    assert service.cmd_status(args, FakeRun()) == 3
    (tmp_path / "pengumon.service").write_text("x")
    monkeypatch.setattr(service, "health", lambda host, port, timeout=3.0: None)
    run = FakeRun({("systemctl", "is-active"): (3, "inactive\n")})
    assert service.cmd_status(args, run) == 1
    assert "system service: stopped" in capsys.readouterr().out
    monkeypatch.setattr(service, "health", lambda host, port, timeout=3.0: "9.9.9")
    run = FakeRun({("systemctl", "is-active"): (0, "active\n")})
    assert service.cmd_status(args, run) == 0
    out = capsys.readouterr().out
    assert "system service: running" in out and "Pengumon 9.9.9" in out


def test_health_reads_the_version_from_the_health_endpoint():
    import http.server
    import json
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/api/health"
            self.send_response(200 if ok else 404)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "version": "1.2.3"}).encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert service.health("127.0.0.1", server.server_address[1]) == "1.2.3"
        assert service.health("0.0.0.0", server.server_address[1]) == "1.2.3"   # a wildcard listen address is asked on loopback
    finally:
        server.shutdown()
    assert service.health("127.0.0.1", server.server_address[1], timeout=0.5) is None


# ---- pengumon run options and the entry points ---------------------------------------------------------------------------------

def test_run_options_set_the_environment_variables(monkeypatch):
    from pengumon import main as app_main
    monkeypatch.delenv("PENGUMON_HOST", raising=False)
    monkeypatch.delenv("PENGUMON_PORT", raising=False)
    monkeypatch.setattr(app_main, "main", lambda: None)
    assert cli.main(["run", "--host", "192.0.2.7", "--port", "9444"]) == 0
    import os
    assert os.environ["PENGUMON_HOST"] == "192.0.2.7" and os.environ["PENGUMON_PORT"] == "9444"
    monkeypatch.delenv("PENGUMON_HOST")
    monkeypatch.delenv("PENGUMON_PORT")


def test_without_a_console_the_streams_go_to_a_log_file_in_the_data_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("PENGUMON_DATA", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    cli._quiet_streams()
    print("hello log")
    sys.stderr.flush()
    stream = sys.stdout
    assert (tmp_path / "state" / "pengumon.log").read_text(encoding="utf-8") == "hello log\n"
    stream.close()


def test_the_log_file_is_rotated_when_it_is_big(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pengumon.log").write_text("x" * (cli.LOG_ROTATE_BYTES + 1))
    monkeypatch.setenv("PENGUMON_DATA", str(state))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    cli._quiet_streams()
    sys.stdout.close()
    assert (state / "pengumon.log.1").stat().st_size == cli.LOG_ROTATE_BYTES + 1
    assert (state / "pengumon.log").stat().st_size == 0


def test_with_a_console_nothing_is_replaced(monkeypatch, tmp_path):
    monkeypatch.setenv("PENGUMON_DATA", str(tmp_path / "state"))
    before = sys.stdout, sys.stderr
    cli._quiet_streams()
    assert (sys.stdout, sys.stderr) == before and not (tmp_path / "state").exists()


def test_the_package_declares_the_console_and_the_windowless_command():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"]["pengumon"] == "pengumon.cli:main"
    assert project["gui-scripts"]["pengumonw"] == "pengumon.cli:main"


def test_windows_install_and_uninstall_end_the_launcher_and_its_python_child():
    """schtasks /End can leave the python process running, which keeps the venv's files open (an upgrade or
    uninstall then fails half way)."""
    for plan in (service.plan_install("windows", USER, python=r"C:\P\Scripts\python.exe", account="PC\\bo"),
                 service.plan_uninstall("windows", USER)):
        commands = _commands(plan)
        assert ["taskkill", "/F", "/T", "/IM", "pengumonw.exe"] in commands
        assert commands.index(["schtasks", "/End", "/TN", "Pengumon"]) < commands.index(list(service.TASKKILL))


def test_plans_for_another_system_use_that_systems_separators():
    """Built on Windows for Linux (or the reverse), a plan must not take the host's path separator."""
    linux = service.plan_install("linux", SYSTEM, python="/opt/pengumon/venv/bin/python")
    assert linux.steps[0].path == "/etc/systemd/system/pengumon.service" and "ExecStart=/opt/pengumon/venv/bin/pengumon run" in linux.steps[0].text
    windows = service.plan_install("windows", USER, python=r"C:\P\Scripts\python.exe", account="PC\\bo")
    assert r"<Command>C:\P\Scripts\pengumonw.exe</Command>" in windows.files["<task.xml>"]
    assert r"<WorkingDirectory>C:\P\Scripts</WorkingDirectory>" in windows.files["<task.xml>"]


# ---- quoting (Stage 2 review) ------------------------------------------------------------------------------------------

def test_systemd_lines_quote_what_would_split_or_start_another_setting():
    text = service.render_systemd_system('/opt/my pengumon/50%/$HOME/"q"/pengumon', {"PENGUMON_HOST": "a b\\c"})
    section = _unit(text)["Service"]
    assert section["ExecStart"] == ['"/opt/my pengumon/50%%/$$HOME/\\"q\\"/pengumon" run']
    assert 'PENGUMON_HOST=a b\\c' not in section["Environment"]
    assert '"PENGUMON_HOST=a b\\\\c"' in section["Environment"]
    assert set(section) == set(_unit(service.render_systemd_system(EXE))["Service"])  # no setting was added


def test_plain_values_stay_unquoted_in_the_unit():
    section = _unit(service.render_systemd_system(EXE, {"PENGUMON_HOST": "::1"}))["Service"]
    assert section["ExecStart"] == [f"{EXE} run"] and "PENGUMON_HOST=::1" in section["Environment"]


@pytest.mark.parametrize("host", ["1.2.3.4\nExecStartPre=/bin/evil", "a\rb"])
def test_a_newline_in_host_is_refused_by_every_plan(host):
    for kind, scope in (("linux", service.SYSTEM), ("linux", service.USER), ("windows", service.USER)):
        with pytest.raises(service.ServiceError, match="newline"):
            service.plan_install(kind, scope, python="/v/bin/python" if kind == "linux" else r"C:\v\Scripts\python.exe",
                                 host=host)


def test_a_path_with_a_newline_is_refused():
    with pytest.raises(service.ServiceError, match="newline"):
        service.plan_install("linux", service.SYSTEM, python="/opt/x\ny/bin/python")


def test_the_windows_task_arguments_quote_the_host():
    plan = service.plan_install("windows", service.USER, python=r"C:\v\Scripts\python.exe", host="a b\" --port 1", port=9000,
                                account=r"D\o")
    arguments = ET.fromstring(plan.files["<task.xml>"]).find("t:Actions/t:Exec/t:Arguments", NS).text
    assert arguments == r'run --host "a b\" --port 1" --port 9000'
    plain = service.plan_install("windows", service.USER, python=r"C:\v\Scripts\python.exe", host="192.0.2.7", port=9000,
                                 account=r"D\o")
    assert ET.fromstring(plain.files["<task.xml>"]).find("t:Actions/t:Exec/t:Arguments", NS).text == "run --host 192.0.2.7 --port 9000"
