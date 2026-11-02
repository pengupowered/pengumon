"""scripts/install.sh, scripts/install.ps1 and scripts/release-assets.sh: what can be checked without installing
anything (the install jobs of ci.yml run the scripts for real on Linux, macOS and Windows). The tests run the shell
scripts as far as they go offline: they must refuse a wheel or lock that does not match SHA256SUMS before they
download or change anything."""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pengumon import service
from pengumon.engine import icmp_linux
from platform_helpers import posix_shell

ROOT = Path(__file__).resolve().parents[1]
SH = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
PS1 = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
RELEASE_ASSETS = ROOT / "scripts" / "release-assets.sh"
VERSION = "9.8.7"


def _sh_pins() -> dict:
    return {m.group(1).lower().replace("_", "-"): m.group(2)
            for m in re.finditer(r'^UV_SHA256_([A-Z0-9_]+)="([0-9a-f]+)"$', SH, re.M)}


def _ps1_pins() -> dict:
    block = re.search(r"\$UvSha256 = @\{(.*?)\n\}", PS1, re.S).group(1)
    return dict(re.findall(r"'([\w-]+)'\s*=\s*'([0-9a-f]+)'", block))


def test_both_scripts_pin_the_same_uv_and_python():
    assert re.search(r'^UV_VERSION="([\d.]+)"$', SH, re.M).group(1) == re.search(r"\$UvVersion = '([\d.]+)'", PS1).group(1)
    assert re.search(r'^PYTHON_VERSION="([\d.]+)"$', SH, re.M).group(1) == re.search(r"\$PythonVersion = '([\d.]+)'", PS1).group(1) == "3.12"


def test_every_uv_archive_has_a_sha256_pin():
    sh, ps1 = _sh_pins(), _ps1_pins()
    assert set(sh) == {"x86-64-unknown-linux-musl", "aarch64-unknown-linux-musl", "aarch64-apple-darwin", "x86-64-apple-darwin"}
    assert set(ps1) == {"x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc"}
    for digest in [*sh.values(), *ps1.values()]:
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert len(set([*sh.values(), *ps1.values()])) == 6  # six different archives, no digest copied twice
    # the target names the script downloads are the ones the pins are for
    for target in ("x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl", "aarch64-apple-darwin", "x86_64-apple-darwin"):
        assert f'UV_TARGET="{target}"' in SH


@pytest.mark.parametrize("script", [SH, PS1], ids=["install.sh", "install.ps1"])
def test_both_scripts_install_from_the_lock_with_hashes_and_wheels_only_then_the_wheel_then_the_service(script):
    assert "--require-hashes" in script and ":all:" in script and "--only-binary" in script
    assert "--no-deps" in script
    assert script.index("--require-hashes") < script.index("service', 'install'" if script is PS1 else "service install")
    assert "SHA256SUMS" in script and "backup" in script and "service" in script and "uninstall" in script
    assert "pengumon-$VERSION.lock" in script.replace("$Version", "$VERSION")
    assert "py3-none-any.whl" in script


def test_the_release_assets_script_names_the_files_the_installers_look_for():
    text = RELEASE_ASSETS.read_text(encoding="utf-8")
    assert 'pengumon-$version.lock' in text and "install.sh" in text and "install.ps1" in text and "SHA256SUMS" in text


def test_install_sh_uses_the_directories_and_the_fix_that_the_service_command_does():
    assert f'APP="{service.LINUX_APP_DIR}"; DATA="{service.SYSTEM_DATA_DIR}"' in SH
    assert f'APP="{service.MAC_APP_DIR}"; DATA="{service.MAC_DAEMON_DATA_DIR}"' in SH
    assert f'SYSCTL_FIX="{icmp_linux.SYSCTL_FIX}"' in SH


def test_the_locks_are_universal():
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-build.txt"):
        header = (ROOT / name).read_text(encoding="utf-8").split("aiosqlite", 1)[0]
        if name == "requirements.txt":
            assert "# platform: universal\n" in header, name


def test_ps1_stays_ascii_so_windows_powershell_5_reads_it_without_a_byte_order_mark():
    PS1.encode("ascii")


# ---- install.sh, offline -----------------------------------------------------------------------------------------------------

@pytest.fixture
def assets(tmp_path):
    folder = tmp_path / "assets"
    folder.mkdir()
    (folder / f"pengumon-{VERSION}-py3-none-any.whl").write_bytes(b"wheel")
    (folder / f"pengumon-{VERSION}.lock").write_text("a==1 --hash=sha256:00\n")
    sums = "".join(f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}\n" for n in sorted(p.name for p in folder.iterdir()))
    (folder / "SHA256SUMS").write_text(sums)
    return folder


def _install(*args):
    # a user install, unless the tests run as root (a container), which only installs for the system; either way the
    # runs below stop before they change anything
    scope = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["--user"]
    return subprocess.run(["sh", str(ROOT / "scripts" / "install.sh"), *scope, "--yes", "--no-browser", *args],
                          capture_output=True, text=True, check=False, cwd=ROOT, timeout=60)


@posix_shell
def test_help_and_syntax():
    assert subprocess.run(["sh", "-n", str(ROOT / "scripts" / "install.sh")], check=False).returncode == 0
    done = _install("--help")
    assert done.returncode == 0 and "--from DIR" in done.stdout


@posix_shell
def test_unknown_options_and_bad_values_are_refused():
    assert _install("--bogus").returncode == 1
    done = _install("--port", "eighty")
    assert done.returncode == 1 and "--port must be a number" in done.stderr
    done = _install("--version", "latest-ish", "--from", ".")
    assert done.returncode == 1


@posix_shell
@pytest.mark.parametrize("version", ["1.2.3.4", "1.2", "1.2.3x", "1..3", ".1.2", "1.2.3.", "1.2.3\n9.9.9", "1.2.3 ", "1.2.3-rc1"])
def test_the_version_is_anchored_like_install_ps1(assets, version):
    done = _install("--version", version, "--from", str(assets))
    assert done.returncode == 1 and "not a version number" in done.stderr, done.stderr


@posix_shell
def test_a_newline_in_a_path_is_refused(tmp_path):
    done = _install("--from", str(tmp_path) + "\nX")
    assert done.returncode == 1 and "must not contain a newline" in done.stderr


def test_an_upgrade_backs_up_as_the_services_account_never_as_root():
    body = SH[SH.index("backup_data() {"):SH.index("confirm() {")]
    assert "systemd-run" in body and "DynamicUser=yes" in body and "User=pengumon" in body
    assert "StateDirectory=pengumon" in body and "--wait --pipe" in body
    assert "sudo -u _pengumon" in body
    assert 'out="$(backup_data 2>&1)"' in SH
    assert "pg backup" not in SH and 'root "$PENGUMON" backup' not in SH


@posix_shell
@pytest.mark.parametrize("victim", [f"pengumon-{VERSION}-py3-none-any.whl", f"pengumon-{VERSION}.lock"])
def test_a_file_that_does_not_match_sha256sums_stops_the_install_before_anything_is_downloaded(assets, victim):
    (assets / victim).write_bytes(b"tampered")
    done = _install("--from", str(assets))
    assert done.returncode == 1
    assert f"{victim} does not match SHA256SUMS" in done.stderr and "nothing was installed" in done.stderr
    assert done.stdout.splitlines() == [f"Pengumon {VERSION}"]  # the version came from the wheel's name; then it stopped
    assert "uv" not in done.stdout.lower().replace("pengumon", "")  # it never got as far as fetching uv


@posix_shell
def test_a_file_that_sha256sums_does_not_list_stops_the_install(assets):
    sums = (assets / "SHA256SUMS").read_text().splitlines()
    (assets / "SHA256SUMS").write_text("\n".join(line for line in sums if ".lock" not in line) + "\n")
    done = _install("--from", str(assets))
    assert done.returncode == 1 and f"SHA256SUMS does not list pengumon-{VERSION}.lock" in done.stderr


@posix_shell
def test_a_missing_file_in_the_folder_is_named(assets):
    (assets / "SHA256SUMS").unlink()
    done = _install("--from", str(assets))
    assert done.returncode == 1 and "SHA256SUMS is missing" in done.stderr


# ---- release-assets.sh ----------------------------------------------------------------------------------------------------------

@posix_shell
@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs GNU sha256sum")
def test_release_assets_collects_the_files_and_lists_a_checksum_for_each(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / f"pengumon-{VERSION}-py3-none-any.whl").write_bytes(b"w")
    (dist / f"pengumon-{VERSION}.tar.gz").write_bytes(b"s")
    out = tmp_path / "out"
    done = subprocess.run(["bash", str(RELEASE_ASSETS), str(dist), str(out)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    names = sorted(p.name for p in out.iterdir())
    assert names == sorted(["SHA256SUMS", "install.ps1", "install.sh", f"pengumon-{VERSION}-py3-none-any.whl",
                            f"pengumon-{VERSION}.lock", f"pengumon-{VERSION}.tar.gz"])
    listed = dict(reversed(line.split("  ")) for line in (out / "SHA256SUMS").read_text().splitlines())
    assert set(listed) == set(names) - {"SHA256SUMS"}
    for name, digest in listed.items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert (out / f"pengumon-{VERSION}.lock").read_text() == (ROOT / "requirements.txt").read_text()
    assert (out / "install.sh").read_text() == SH


@posix_shell
def test_release_assets_refuses_a_dist_folder_without_exactly_one_wheel(tmp_path):
    (tmp_path / "dist").mkdir()
    done = subprocess.run(["bash", str(RELEASE_ASSETS), str(tmp_path / "dist"), str(tmp_path / "out")],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 1 and "exactly one pengumon-*.whl" in done.stderr
