"""`pengumon service install | uninstall | status`: run Pengumon in the background, started by the system.

    Linux     a systemd system unit (as root: DynamicUser, CAP_NET_RAW only, ProtectSystem=strict, the venv in
              /opt/pengumon, data in /var/lib/pengumon), or a systemd user unit when not root (--user)
    macOS     a LaunchDaemon with a dedicated user `_pengumon` (as root), or a LaunchAgent (--user)
    Windows   a Task Scheduler task for the current user, started at logon, running `pengumonw` (no console window)

Every operating system's definition is a pure function of its inputs (`render_*`), and `plan_install` /
`plan_uninstall` turn them into a list of steps that `run_plan` carries out (or `--dry-run` prints). Nothing here
needs more than the standard library. Data directories follow pengumon.paths: the system units set PENGUMON_DATA,
the user ones use the per-user default.
"""

import getpass
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

from pengumon import __version__, paths

NAME = "pengumon"
UNIT = "pengumon.service"
LABEL = "com.pengupowered.pengumon"
TASK = "Pengumon"
MAC_USER = "_pengumon"
# Ending the task may leave the python process under the launcher running, which keeps its files open (and the
# folder undeletable); this ends the launcher and everything it started.
TASKKILL = ("taskkill", "/F", "/T", "/IM", "pengumonw.exe")

# Paths of another system than the one running (a plan for Linux built on Windows) use the pure classes: no
# host-dependent separators.
SYSTEM_UNIT_PATH = PurePosixPath("/etc/systemd/system") / UNIT
SYSTEM_DATA_DIR = "/var/lib/pengumon"  # StateDirectory=pengumon
LINUX_APP_DIR = "/opt/pengumon"
MAC_APP_DIR = "/usr/local/pengumon"
MAC_DAEMON_PLIST = PurePosixPath("/Library/LaunchDaemons") / f"{LABEL}.plist"
MAC_DAEMON_DATA_DIR = "/Library/Application Support/Pengumon"

# systemd-analyze security --offline scores the system unit below this exposure (CI checks it on a real runner).
MAX_EXPOSURE = 2.0

SYSTEM, USER = "system", "user"


class ServiceError(Exception):
    """Something the user has to fix (not root, wrong platform, a tool is missing); printed as one line."""


# ---- definitions: pure functions ------------------------------------------------------------------------------------

_SD_PLAIN = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:@+=,-")


def _no_control(value: str, what: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ServiceError(f"{what} must not contain a newline or other control character")
    return value


def _sd_word(value: str, *, exec_line: bool = False) -> str:
    """One word of a systemd unit line (systemd.syntax quoting): `%` is a specifier, and in ExecStart `$` starts a
    variable, so both are doubled; a word with anything but plain characters is double-quoted with `\\` and `"`
    escaped, so a space or a quote can never split it or start another setting."""
    _no_control(value, "A value in the service unit")
    out = value.replace("%", "%%")
    if exec_line:
        out = out.replace("$", "$$")
    if all(c in _SD_PLAIN for c in value):
        return out
    return '"' + out.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _exec_start(exe: str) -> str:
    return f"ExecStart={_sd_word(exe, exec_line=True)} run"


def _condition_exists(exe: str) -> str:
    """Start only while the program is there: after `uv tool uninstall` (or deleting the folder) the unit is skipped
    instead of failing with status 203/EXEC every five seconds, forever."""
    _no_control(exe, "The path of pengumon")
    return "ConditionPathExists=" + exe.replace("%", "%%")


def _environment(env: Dict[str, str]) -> List[str]:
    lines = []
    for key, value in env.items():
        _no_control(key + value, "A value in the service unit")
        assignment = f"{key}={value}"
        # one quoted assignment: Environment="KEY=value with spaces"
        lines.append("Environment=" + _sd_word(assignment))
    return lines


def render_systemd_system(exe: str, env: Optional[Dict[str, str]] = None) -> str:
    """The system unit. DynamicUser gives it a throw-away uid, StateDirectory a data directory it alone can read,
    and CAP_NET_RAW is the only privilege (the raw ICMP socket; no ping_group_range sysctl needed). The venv lives
    outside /home because ProtectHome hides it."""
    env = {"PENGUMON_DATA": SYSTEM_DATA_DIR, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    lines = [
        "[Unit]",
        "Description=Pengumon, home network monitor",
        "Documentation=https://github.com/pengupowered/pengumon",
        "After=network-online.target",
        "Wants=network-online.target",
        _condition_exists(exe),
        "",
        "[Service]",
        "Type=simple",
        _exec_start(exe),
        *_environment(env),
        "Restart=on-failure",
        "RestartSec=5",
        "TimeoutStopSec=20",
        "",
        "# Who it runs as, and what it may write",
        "DynamicUser=yes",
        f"StateDirectory={NAME}",
        "StateDirectoryMode=0700",
        "UMask=0077",
        "",
        "# The one privilege: raw ICMP sockets",
        "AmbientCapabilities=CAP_NET_RAW",
        "CapabilityBoundingSet=CAP_NET_RAW",
        "NoNewPrivileges=yes",
        "",
        "# The rest of the system is read-only or out of sight",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "ProtectProc=invisible",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "RemoveIPC=yes",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "SystemCallFilter=~@privileged @resources",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def render_systemd_user(exe: str, env: Optional[Dict[str, str]] = None) -> str:
    """The user unit: runs as you, so the data directory is the per-user default. No mount or user-namespace
    sandboxing (ProtectSystem, PrivateTmp): on Ubuntu 24.04 and others an unprivileged user manager is not allowed
    to create the namespaces and the unit would not start. What needs none is on."""
    env = {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    lines = [
        "[Unit]",
        "Description=Pengumon, home network monitor",
        "Documentation=https://github.com/pengupowered/pengumon",
        "After=network-online.target",
        _condition_exists(exe),
        "",
        "[Service]",
        "Type=simple",
        _exec_start(exe),
        *_environment(env),
        "Restart=on-failure",
        "RestartSec=5",
        "TimeoutStopSec=20",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "SystemCallArchitectures=native",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def render_launchd(exe: str, env: Dict[str, str], log: str, user: Optional[str] = None) -> str:
    """A launchd property list: starts at load (boot for a daemon, login for an agent) and restarts if it exits."""
    plist: Dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [exe, "run"],
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", **env},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    if user:
        plist["UserName"] = user
        plist["GroupName"] = user
    return plistlib.dumps(plist, sort_keys=False).decode("utf-8")


def render_task_xml(command: str, arguments: str, account: str, working_dir: str) -> str:
    """The Task Scheduler definition: at logon of `account`, no admin rights, no time limit, restarted if it dies,
    also on battery."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Pengumon, home network monitor (starts when you sign in)</Description>
    <URI>\\{TASK}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(account)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(account)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


# ---- the plan: what install / uninstall do, as data -----------------------------------------------------------------

@dataclass
class Step:
    """One effect. kind: write (path, text, mode), mkdir (path, mode), remove (path), run (argv; tolerate = a
    failure is fine), mac_user (create the _pengumon account if it is missing), chown (path, owner)."""

    kind: str
    path: Optional[str] = None
    text: Optional[str] = None
    mode: int = 0o644
    argv: Sequence[str] = ()
    tolerate: bool = False
    owner: Optional[str] = None

    def describe(self) -> str:
        if self.kind == "write":
            return f"write {self.path} (mode {self.mode:o})"
        if self.kind == "mkdir":
            return f"create directory {self.path} (mode {self.mode:o})"
        if self.kind == "remove":
            return f"remove {self.path}"
        if self.kind == "chown":
            return f"give {self.path} to {self.owner}"
        if self.kind == "mac_user":
            return f"create the account {MAC_USER} (no login, no home) if it does not exist"
        return "run " + " ".join(_quote(a) for a in self.argv) + ("  (a failure is fine)" if self.tolerate else "")


@dataclass
class Plan:
    mode: str                     # "systemd-system", "systemd-user", "launchd-daemon", "launchd-agent", "windows-task"
    steps: List[Step] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)  # printed after the steps ran
    files: Dict[str, str] = field(default_factory=dict)  # path -> content, for --dry-run and tests


def _quote(arg: str) -> str:
    return f"'{arg}'" if (not arg or any(c in arg for c in " \t'\"$")) else arg


def _win_arg(value: str) -> str:
    """One command-line argument for the task's Arguments (CommandLineToArgvW rules): quoted when it has anything
    but plain characters, so a space cannot start another argument."""
    _no_control(value, "A value in the service definition")
    if value and all(c in _SD_PLAIN or c == ":" for c in value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def _env_for(host: Optional[str], port: Optional[int]) -> Dict[str, str]:
    env = {}
    if host:
        env["PENGUMON_HOST"] = _no_control(host, "--host")
    if port:
        env["PENGUMON_PORT"] = str(int(port))
    return env


_UNIT_ENV = re.compile(r'^Environment="?(PENGUMON_HOST|PENGUMON_PORT)=([^"\n]*)"?\s*$', re.MULTILINE)
_TASK_ARG = re.compile(r'--(host|port)\s+("[^"]*"|\S+)')


def existing_settings(kind: str, scope: str, *, home: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
                      run: Optional[Callable] = None) -> Dict[str, str]:
    """The listen address (PENGUMON_HOST, PENGUMON_PORT) an installed service was given with --host and --port, read
    from its definition; {} when there is none or it sets neither. An upgrade passes them on, so installing again does
    not quietly close the dashboard to the other devices."""
    home = home or Path.home()
    found: Dict[str, str] = {}
    try:
        if kind == "linux":
            path = Path(SYSTEM_UNIT_PATH) if scope == SYSTEM else systemd_user_unit_path(home, env)
            for key, value in _UNIT_ENV.findall(path.read_text(encoding="utf-8")):
                found[key] = value.replace("%%", "%")
        elif kind == "macos":
            path = Path(MAC_DAEMON_PLIST) if scope == SYSTEM else launch_agent_path(home)
            with open(path, "rb") as handle:
                variables = plistlib.load(handle).get("EnvironmentVariables") or {}
            found = {k: str(v) for k, v in variables.items() if k in ("PENGUMON_HOST", "PENGUMON_PORT")}
        else:
            done = (run or default_runner)(["schtasks", "/Query", "/TN", TASK, "/XML"])
            if done.returncode == 0:
                from xml.sax.saxutils import unescape
                match = re.search(r"<Arguments>(.*?)</Arguments>", done.stdout, re.DOTALL)
                for name, value in _TASK_ARG.findall(unescape(match.group(1)) if match else ""):
                    found["PENGUMON_" + name.upper()] = value.strip('"')
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    if "PENGUMON_PORT" in found and not found["PENGUMON_PORT"].isdigit():
        del found["PENGUMON_PORT"]
    return found


def service_data_dir(kind: str) -> str:
    """Where the system service keeps its data (its unit sets PENGUMON_DATA to this)."""
    if kind == "linux":
        return SYSTEM_DATA_DIR
    if kind == "macos":
        return MAC_DAEMON_DATA_DIR
    raise ServiceError("Windows has no system service: its task runs as you, with the data in the usual place")


def system_kind(platform: Optional[str] = None) -> str:
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return "linux"
    if platform == "darwin":
        return "macos"
    if platform == "win32":
        return "windows"
    raise ServiceError(f"no service support for {platform}; run `pengumon run` yourself")


def default_scope(kind: str, is_root: bool) -> str:
    """Linux and macOS: the system service for root, the per-user one otherwise. Windows has only the per-user task."""
    return SYSTEM if kind != "windows" and is_root else USER


def systemd_user_unit_path(home: Path, env: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME")
    config = Path(base) if base and Path(base).is_absolute() else home / ".config"
    return config / "systemd" / "user" / UNIT


def launch_agent_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _exe(python: Optional[str], windows: bool) -> str:
    """The console script next to the interpreter running this: `pengumon` (`pengumonw` on Windows, which starts
    without a console window)."""
    name = "pengumonw.exe" if windows else NAME
    if python is None:  # the interpreter running this: a path of this host, whatever system the plan is for
        return _no_control(str(Path(sys.executable).parent / name), "The path of pengumon")
    pure = PureWindowsPath if windows else PurePosixPath
    return _no_control(str(pure(python).parent / name), "The path of pengumon")


def plan_install(
    kind: str, scope: str, *, python: Optional[str] = None, home: Optional[Path] = None, host: Optional[str] = None,
    port: Optional[int] = None, account: Optional[str] = None, env: Optional[Dict[str, str]] = None,
    linger: Optional[bool] = None,
) -> Plan:
    """What `service install` does for this OS and scope. `python` is the interpreter whose neighbour is the
    console script (default: the running one); `linger` is whether the user's services survive logout (Linux user
    unit; None = unknown)."""
    home = home or Path.home()
    extra = _env_for(host, port)
    if kind == "linux" and scope == SYSTEM:
        exe = _exe(python, False)
        text = render_systemd_system(exe, extra)
        return Plan("systemd-system", [
            Step("write", str(SYSTEM_UNIT_PATH), text, 0o644),
            Step("run", argv=["systemctl", "daemon-reload"]),
            Step("run", argv=["systemctl", "enable", UNIT]),
            Step("run", argv=["systemctl", "restart", UNIT]),
        ], [f"The service runs as a throw-away user; its data is in {SYSTEM_DATA_DIR}."],
            {str(SYSTEM_UNIT_PATH): text})
    if kind == "linux":
        exe = _exe(python, False)
        unit = systemd_user_unit_path(home, env)
        text = render_systemd_user(exe, extra)
        notes = [f"Check that it can probe: {exe} doctor"]
        if "/uv/tools/" in exe:
            notes.append("After `uv tool install` of a newer version, run `pengumon service install` again to restart it on the new "
                         "version. Before `uv tool uninstall pengumon`, run `pengumon service uninstall --user`.")
        if linger is False:
            notes.insert(0, "It stops when you log out. To keep it running: sudo loginctl enable-linger "
                         + (account or "$USER"))
        return Plan("systemd-user", [
            Step("write", str(unit), text, 0o644),
            Step("run", argv=["systemctl", "--user", "daemon-reload"]),
            Step("run", argv=["systemctl", "--user", "enable", UNIT]),
            Step("run", argv=["systemctl", "--user", "restart", UNIT]),
        ], notes, {str(unit): text})
    if kind == "macos" and scope == SYSTEM:
        exe = _exe(python, False)
        data = MAC_DAEMON_DATA_DIR
        log = f"{data}/pengumon.log"
        text = render_launchd(exe, {"PENGUMON_DATA": data, **extra}, log, MAC_USER)
        return Plan("launchd-daemon", [
            Step("mac_user"),
            Step("mkdir", data, mode=0o700, owner=MAC_USER),
            Step("write", str(MAC_DAEMON_PLIST), text, 0o644),
            Step("chown", str(MAC_DAEMON_PLIST), owner="root:wheel"),
            Step("run", argv=["launchctl", "bootout", f"system/{LABEL}"], tolerate=True),
            Step("run", argv=["launchctl", "bootstrap", "system", str(MAC_DAEMON_PLIST)]),
            Step("run", argv=["launchctl", "enable", f"system/{LABEL}"]),
            Step("run", argv=["launchctl", "kickstart", "-k", f"system/{LABEL}"], tolerate=True),
        ], [f"It runs as the account {MAC_USER}; its data is in {data}. Log: {log}"], {str(MAC_DAEMON_PLIST): text})
    if kind == "macos":
        exe = _exe(python, False)
        plist = launch_agent_path(home)
        log = str(home / "Library" / "Logs" / "Pengumon" / "pengumon.log")
        text = render_launchd(exe, extra, log)
        domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"
        return Plan("launchd-agent", [
            Step("mkdir", str(Path(log).parent), mode=0o700),
            Step("write", str(plist), text, 0o644),
            Step("run", argv=["launchctl", "bootout", f"{domain}/{LABEL}"], tolerate=True),
            Step("run", argv=["launchctl", "bootstrap", domain, str(plist)]),
            Step("run", argv=["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], tolerate=True),
        ], ["As a login agent, macOS may ask once whether Pengumon may find devices on your local network. "
            "Allow it, or use `sudo pengumon service install` for a system service, which is not asked."],
            {str(plist): text})
    # Windows
    exe = _exe(python, True)
    arguments = "run" + (f" --host {_win_arg(host)}" if host else "") + (f" --port {int(port)}" if port else "")
    xml = render_task_xml(exe, arguments, account or windows_account(), str(PureWindowsPath(exe).parent))
    return Plan("windows-task", [
        Step("run", argv=["schtasks", "/End", "/TN", TASK], tolerate=True),
        Step("run", argv=list(TASKKILL), tolerate=True),
        Step("write", "<task.xml>", xml, 0o600),
        Step("run", argv=["schtasks", "/Create", "/TN", TASK, "/XML", "<task.xml>", "/F"]),
        Step("run", argv=["schtasks", "/Run", "/TN", TASK]),
    ], ["It starts at every sign-in, without a console window."], {"<task.xml>": xml})


def plan_uninstall(kind: str, scope: str, *, home: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Plan:
    """What `service uninstall` does. The data directory and the venv are left alone."""
    home = home or Path.home()
    if kind == "linux" and scope == SYSTEM:
        return Plan("systemd-system", [
            Step("run", argv=["systemctl", "disable", "--now", UNIT], tolerate=True),
            Step("remove", str(SYSTEM_UNIT_PATH)),
            Step("run", argv=["systemctl", "daemon-reload"]),
        ], [f"Your data is kept in {SYSTEM_DATA_DIR}."])
    if kind == "linux":
        unit = systemd_user_unit_path(home, env)
        return Plan("systemd-user", [
            Step("run", argv=["systemctl", "--user", "disable", "--now", UNIT], tolerate=True),
            Step("remove", str(unit)),
            Step("run", argv=["systemctl", "--user", "daemon-reload"]),
        ], [f"Your data is kept in {paths.data_dir()}."])
    if kind == "macos" and scope == SYSTEM:
        return Plan("launchd-daemon", [
            Step("run", argv=["launchctl", "bootout", f"system/{LABEL}"], tolerate=True),
            Step("remove", str(MAC_DAEMON_PLIST)),
        ], [f"Your data is kept in {MAC_DAEMON_DATA_DIR}, and the account {MAC_USER} stays (it owns that data)."])
    if kind == "macos":
        domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"
        return Plan("launchd-agent", [
            Step("run", argv=["launchctl", "bootout", f"{domain}/{LABEL}"], tolerate=True),
            Step("remove", str(launch_agent_path(home))),
        ], [f"Your data is kept in {paths.data_dir()}."])
    return Plan("windows-task", [
        Step("run", argv=["schtasks", "/End", "/TN", TASK], tolerate=True),
        Step("run", argv=list(TASKKILL), tolerate=True),
        Step("run", argv=["schtasks", "/Delete", "/TN", TASK, "/F"]),
    ], [f"Your data is kept in {paths.data_dir()}."])


def windows_account(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    user = env.get("USERNAME") or getpass.getuser()
    domain = env.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


# ---- carrying a plan out ---------------------------------------------------------------------------------------------

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # the file is created with its final mode, never world-readable for a moment
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.chmod(path, mode)


def _ensure_mac_user(run: Runner) -> None:
    if run(["dscl", ".", "-read", f"/Users/{MAC_USER}", "UniqueID"]).returncode == 0:
        return
    listing = run(["dscl", ".", "-list", "/Users", "UniqueID"]).stdout
    used = {int(parts[1]) for parts in (line.split() for line in listing.splitlines()) if len(parts) == 2 and parts[1].isdigit()}
    uid = next((n for n in range(450, 500) if n not in used), None)  # the range Apple's service accounts use
    if uid is None:
        raise ServiceError(f"no free user id between 450 and 499 for the account {MAC_USER}")
    path, group = f"/Users/{MAC_USER}", f"/Groups/{MAC_USER}"
    for argv in (
        ["dscl", ".", "-create", group],
        ["dscl", ".", "-create", group, "PrimaryGroupID", str(uid)],
        ["dscl", ".", "-create", path],
        ["dscl", ".", "-create", path, "UniqueID", str(uid)],
        ["dscl", ".", "-create", path, "PrimaryGroupID", str(uid)],
        ["dscl", ".", "-create", path, "UserShell", "/usr/bin/false"],
        ["dscl", ".", "-create", path, "RealName", "Pengumon"],
        ["dscl", ".", "-create", path, "NFSHomeDirectory", "/var/empty"],
        ["dscl", ".", "-create", path, "IsHidden", "1"],
    ):
        done = run(argv)
        if done.returncode != 0:
            raise ServiceError(f"{' '.join(argv)} failed: {(done.stderr or done.stdout).strip()}")


def run_plan(plan: Plan, run: Runner = default_runner, out=None) -> None:
    """Carries the steps out in order; the first failing step (one that is not `tolerate`) stops with a ServiceError."""
    out = out or sys.stdout
    task_file: Optional[Path] = None
    try:
        for step in plan.steps:
            print(f"  {step.describe()}", file=out)
            if step.kind == "write" and step.path == "<task.xml>":
                handle, name = tempfile.mkstemp(suffix=".xml", prefix="pengumon-task-")
                os.close(handle)
                task_file = Path(name)
                task_file.write_bytes(b"\xff\xfe" + str(step.text).encode("utf-16-le"))  # Task Scheduler wants UTF-16
            elif step.kind == "write":
                _write(Path(step.path), step.text or "", step.mode)
            elif step.kind == "mkdir":
                Path(step.path).mkdir(mode=step.mode, parents=True, exist_ok=True)
                if step.owner:
                    shutil.chown(step.path, user=step.owner)
                    os.chmod(step.path, step.mode)
            elif step.kind == "chown":
                done = run(["chown", step.owner, step.path])
                if done.returncode != 0:
                    raise ServiceError(f"chown {step.owner} {step.path} failed: {done.stderr.strip()}")
            elif step.kind == "remove":
                Path(step.path).unlink(missing_ok=True)
            elif step.kind == "mac_user":
                _ensure_mac_user(run)
            else:
                argv = [str(task_file) if a == "<task.xml>" and task_file else a for a in step.argv]
                done = run(argv)
                if done.returncode != 0 and not step.tolerate:
                    raise ServiceError(f"{' '.join(argv)} failed ({done.returncode}): "
                                       f"{(done.stderr or done.stdout).strip() or 'no output'}")
    finally:
        if task_file:
            task_file.unlink(missing_ok=True)
    for note in plan.notes:
        print(note, file=out)


# ---- status ---------------------------------------------------------------------------------------------------------

def health(host: str, port: int, timeout: float = 3.0) -> Optional[str]:
    """The version /api/health reports, or None when nothing healthy answers."""
    body = health_body(host, port, timeout)
    return body.get("version") if body else None


def health_body(host: str, port: int, timeout: float = 3.0) -> Optional[dict]:
    """What /api/health says (status "ok", version, probing), or None when nothing healthy answers."""
    import json

    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with urllib.request.urlopen(f"http://{shown}:{port}/api/health", timeout=timeout) as reply:
            body = json.loads(reply.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return body if isinstance(body, dict) and body.get("status") == "ok" else None


def installed_scopes(kind: str, home: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
                     run: Runner = default_runner) -> List[str]:
    """Which services exist on this machine: [] , ["system"], ["user"] or both."""
    home = home or Path.home()
    found = []
    if kind == "linux":
        if Path(SYSTEM_UNIT_PATH).is_file():
            found.append(SYSTEM)
        if systemd_user_unit_path(home, env).is_file():
            found.append(USER)
    elif kind == "macos":
        if Path(MAC_DAEMON_PLIST).is_file():
            found.append(SYSTEM)
        if launch_agent_path(home).is_file():
            found.append(USER)
    elif run(["schtasks", "/Query", "/TN", TASK]).returncode == 0:
        found.append(USER)
    return found


def manager_state(kind: str, scope: str, run: Runner = default_runner) -> str:
    """What the service manager says: "running", "stopped" or "unknown"."""
    if kind == "linux":
        argv = ["systemctl", *(["--user"] if scope == USER else []), "is-active", UNIT]
        return "running" if run(argv).stdout.strip() == "active" else "stopped"
    if kind == "macos":
        domain = "system" if scope == SYSTEM else f"gui/{os.getuid()}"
        done = run(["launchctl", "print", f"{domain}/{LABEL}"])
        if done.returncode != 0:
            return "stopped"
        return "running" if "state = running" in done.stdout else "stopped"
    done = run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"])
    return "running" if "Running" in done.stdout else "stopped" if done.returncode == 0 else "unknown"


# ---- command line ----------------------------------------------------------------------------------------------------

def _scope_from(args, kind: str) -> str:
    if getattr(args, "user", False) and getattr(args, "system", False):
        raise ServiceError("--user and --system exclude each other")
    if getattr(args, "system", False):
        return SYSTEM
    if getattr(args, "user", False):
        return USER
    return default_scope(kind, hasattr(os, "geteuid") and os.geteuid() == 0)


def _require_root(kind: str, scope: str, action: str) -> None:
    if scope == SYSTEM and hasattr(os, "geteuid") and os.geteuid() != 0:
        raise ServiceError(f"{action} the system service needs root: run it with sudo, "
                           "or add --user for a service that runs as you")
    if kind == "linux" and scope == USER and hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ServiceError("--user as root would install into root's own user manager; leave --user out for "
                           "the system service, or run it as the user who should own the service")


def _linger(run: Runner) -> Optional[bool]:
    done = run(["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"])
    if done.returncode != 0:
        return None
    return done.stdout.strip() == "yes"


def cmd_install(args, run: Runner = default_runner) -> int:
    kind = system_kind()
    scope = _scope_from(args, kind)
    if not args.dry_run:
        _require_root(kind, scope, "installing")
    exe = _exe(None, kind == "windows")
    if not args.dry_run and not Path(exe).exists():
        raise ServiceError(f"{exe} does not exist: run this from the pengumon that is installed in a virtual environment")
    home = Path.home()
    kept = existing_settings(kind, scope, home=home, run=run)
    host = args.host or kept.get("PENGUMON_HOST")
    port = args.port or (int(kept["PENGUMON_PORT"]) if kept.get("PENGUMON_PORT") else None)
    if (host, port) != (args.host, args.port):
        kept_words = ", ".join(w for w in (f"host {host}" if host != args.host else "",
                                           f"port {port}" if port != args.port else "") if w)
        print(f"Keeping the installed service's listen address ({kept_words}). Add --host or --port to change it.")
    plan = plan_install(kind, scope, home=home, host=host, port=port,
                        account=getpass.getuser(), linger=_linger(run) if kind == "linux" and scope == USER else None)
    print(f"{'Would install' if args.dry_run else 'Installing'} Pengumon {__version__} as a service ({plan.mode}):")
    if args.dry_run:
        for step in plan.steps:
            print(f"  {step.describe()}")
        for name, text in plan.files.items():
            print(f"\n--- {name} ---\n{text.rstrip()}")
        return 0
    run_plan(plan, run)
    host, port, _, _ = paths.listen_address(None, {**os.environ, **_env_for(host, port)})
    print(f"Pengumon is starting; open http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/")
    if host in ("0.0.0.0", "::"):
        for line in _other_devices_lines(port, _token_is_set(kind, scope, home)):
            print(line)
    return 0


def _token_is_set(kind: str, scope: str, home: Path) -> bool:
    """Whether an access token is already given to this service (or to this shell): its unit, a systemd drop-in
    (`systemctl edit`), a launchd plist or the environment mention PENGUMON_ACCESS_TOKEN. False when unknown."""
    if os.environ.get("PENGUMON_ACCESS_TOKEN"):
        return True
    try:
        if kind == "linux":
            unit = Path(SYSTEM_UNIT_PATH) if scope == SYSTEM else systemd_user_unit_path(home)
            files = [unit, *sorted(unit.parent.joinpath(unit.name + ".d").glob("*.conf"))]
        elif kind == "macos":
            files = [Path(MAC_DAEMON_PLIST) if scope == SYSTEM else launch_agent_path(home)]
        else:
            return False
        return any("PENGUMON_ACCESS_TOKEN" in f.read_text(encoding="utf-8", errors="replace") for f in files)
    except OSError:
        return False


def _other_devices_lines(port: int, has_token: bool) -> List[str]:
    """What to tell the user after `--host 0.0.0.0`: the address other devices use (this computer's name, and its
    address on the network when it can be found the way the dashboard's header finds it), then the token advice."""
    import socket

    from pengumon.engine.netinfo import get_lan_ips
    urls = []
    try:
        urls.append(f"http://{socket.gethostname()}:{port}/")
    except OSError:
        pass
    try:
        urls.extend(f"http://{ip}:{port}/" for ip in get_lan_ips()[:1])
    except Exception:  # noqa: BLE001 - a lookup that fails only means one line fewer
        pass
    lines = []
    if urls:
        lines.append("From other devices on your network, open " + " or ".join(urls))
    lines.append("It listens on every network connection of this computer, so anyone on the network can reach it."
                 + (" The access token you set protects it." if has_token
                    else " Set an access token first (see docs/install.md)."))
    return lines


def cmd_uninstall(args, run: Runner = default_runner) -> int:
    kind = system_kind()
    if getattr(args, "system", False) or getattr(args, "user", False):
        scopes = [_scope_from(args, kind)]
    else:
        scopes = installed_scopes(kind, run=run)
    if not scopes:
        print("Pengumon is not installed as a service.")
        return 0
    for scope in scopes:
        if not args.dry_run:
            _require_root(kind, scope, "removing")
        plan = plan_uninstall(kind, scope)
        print(f"{'Would remove' if args.dry_run else 'Removing'} the Pengumon service ({plan.mode}):")
        if args.dry_run:
            for step in plan.steps:
                print(f"  {step.describe()}")
            continue
        run_plan(plan, run)
    return 0


def cmd_status(args, run: Runner = default_runner) -> int:
    """0: installed, running and answering. 1: installed but not answering. 3: not installed."""
    kind = system_kind()
    scopes = installed_scopes(kind, run=run)
    if not scopes:
        print("Pengumon is not installed as a service. Install it with: pengumon service install")
        return 3
    healthy = False
    for scope in scopes:
        state = manager_state(kind, scope, run)
        print(f"{scope} service: {state}")
    listen_env = dict(os.environ)
    for scope in scopes:  # the port the service was installed with, unless this command is told another
        for key, value in existing_settings(kind, scope, run=run).items():
            listen_env.setdefault(key, value)
    host, port, _, _ = paths.listen_address(None, listen_env)
    body = health_body(host, port)
    if body:
        healthy = True
        version = body.get("version")
        print(f"answering on port {port}: Pengumon {version}")
        if version and version != __version__:
            print(f"The program on disk is Pengumon {__version__}, but the running service is {version}. "
                  "Run `pengumon service install` to restart it on the new version.")
        if body.get("probing") == "unavailable":
            print(f"NOT PROBING: {body.get('probing_problem') or 'this computer cannot send pings'}. "
                  "Pengumon is running but measures nothing"
                  + (f". To fix it, run this once: {body['probing_fix']}" if body.get("probing_fix") else
                     ". Run `pengumon doctor` for the fix."))
    else:
        print(f"nothing healthy answers on port {port} (if the service listens elsewhere, set PENGUMON_PORT for this command)")
    return 0 if healthy else 1


def add_parser(sub) -> None:
    """Adds `service` with its three subcommands to cli's subparsers."""
    service = sub.add_parser("service", help="start Pengumon automatically: install, uninstall or show the status")
    actions = service.add_subparsers(dest="service_command", metavar="{install,uninstall,status}", required=True)
    install = actions.add_parser("install", help="install and start the background service")
    install.add_argument("--system", action="store_true", help="the system-wide service (needs root; the default as root)")
    install.add_argument("--user", action="store_true", help="a service that runs as you (the default when not root)")
    install.add_argument("--host", help="listen address (default 127.0.0.1)")
    install.add_argument("--port", type=int, help="listen port (default 8000)")
    install.add_argument("--dry-run", action="store_true", help="show what would be done and the files, change nothing")
    install.set_defaults(func=cmd_install)
    uninstall = actions.add_parser("uninstall", help="stop and remove the service (keeps your data)")
    uninstall.add_argument("--system", action="store_true")
    uninstall.add_argument("--user", action="store_true")
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.set_defaults(func=cmd_uninstall)
    actions.add_parser("status", help="is the service installed, running and answering").set_defaults(func=cmd_status)
