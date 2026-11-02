"""Installing and releasing: the release compose file, the changelog, the GitHub workflows and the dependency gate in
scripts/deploy.sh. These files sit at the repository root, so Dockerfile.test copies them in."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from pengumon import __version__

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML reads the key `on` as True
    return data


def _steps(workflow: dict):
    for job in workflow["jobs"].values():
        yield from job.get("steps", [])


def test_release_compose_pins_the_current_version():
    compose = yaml.safe_load((ROOT / "compose.release.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["pengumon"]
    assert service["image"] == f"ghcr.io/pengupowered/pengumon:{__version__}"
    assert "build" not in service
    assert service["network_mode"] == "host"
    assert service["cap_add"] == ["NET_RAW"]
    assert "./config.yaml:/app/config.yaml:ro" in service["volumes"]


@pytest.mark.parametrize("name", ["docker-compose.yml", "compose.release.yaml"])
def test_both_compose_files_run_with_a_read_only_root_and_a_tmpfs(name):
    """The image writes only to the data volume and /tmp (scripts/test-container.sh checks that on the running image)."""
    service = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["services"]["pengumon"]
    assert service["read_only"] is True
    assert service["tmpfs"] == ["/tmp"]
    assert any(v.startswith("pengumon_data:/app/data") for v in service["volumes"])


def test_release_compose_passes_the_same_environment_as_the_build_compose():
    release = yaml.safe_load((ROOT / "compose.release.yaml").read_text(encoding="utf-8"))["services"]["pengumon"]
    build = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["pengumon"]
    assert release["environment"] == build["environment"]


def test_the_repository_has_no_file_named_compose_yaml():
    """Docker and Podman prefer compose.yaml over docker-compose.yml, which would hijack the source build and deploy.sh."""
    assert not (ROOT / "compose.yaml").exists()


def test_changelog_has_unreleased_and_a_section_for_the_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    notes = subprocess.run(
        [str(ROOT / "scripts" / "changelog-section.sh"), __version__], capture_output=True, text=True, check=False
    )
    assert notes.returncode == 0, notes.stderr
    assert notes.stdout.strip()
    assert "]: https://" not in notes.stdout  # the link references at the bottom are not release notes


def test_changelog_section_fails_for_an_unknown_version():
    res = subprocess.run(
        [str(ROOT / "scripts" / "changelog-section.sh"), "0.0.0"], capture_output=True, text=True, check=False
    )
    assert res.returncode == 1


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflows_pin_actions_to_commit_shas_and_default_to_read_only(name):
    workflow = _load(WORKFLOWS / name)
    assert workflow["permissions"] == {"contents": "read"}
    for step in _steps(workflow):
        uses = step.get("uses")
        if uses:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), f"{uses} is not pinned to a commit SHA"
    for job in workflow["jobs"].values():
        if "uses" in job:  # a reusable workflow of this repository
            assert job["uses"].startswith("./.github/workflows/")


def test_ci_runs_on_push_and_pull_request_and_can_be_called():
    triggers = _load(WORKFLOWS / "ci.yml")["on"]
    assert {"push", "pull_request", "workflow_call"} <= set(triggers)


def test_release_runs_the_tests_first_and_only_publishes_with_the_permissions_it_needs():
    workflow = _load(WORKFLOWS / "release.yml")
    assert workflow["on"]["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]
    jobs = workflow["jobs"]
    assert jobs["test"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["image"]["needs"] == "test"
    assert jobs["image"]["permissions"] == {"contents": "read", "packages": "write"}
    assert jobs["manifest"]["needs"] == "image"
    assert jobs["manifest"]["permissions"] == {"contents": "read", "packages": "write"}
    assert jobs["release"]["needs"] == "manifest"
    assert jobs["release"]["permissions"] == {"contents": "write"}


def test_release_builds_each_architecture_natively_from_the_tested_wheel_and_joins_them():
    jobs = _load(WORKFLOWS / "release.yml")["jobs"]
    runners = {m["arch"]: m["os"] for m in jobs["image"]["strategy"]["matrix"]["include"]}
    assert runners == {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
    uses = [str(s.get("uses", "")) for job in jobs.values() for s in job.get("steps", [])]
    assert not any(u.startswith("docker/setup-qemu-action@") for u in uses)  # no emulation
    build = next(s for s in jobs["image"]["steps"] if str(s.get("uses", "")).startswith("docker/build-push-action@"))
    assert build["with"]["platforms"] == "linux/${{ matrix.arch }}"
    assert build["with"]["build-args"] == "WHEEL_STAGE=wheel-prebuilt"
    assert "org.opencontainers.image.licenses=MIT" in build["with"]["labels"]
    downloads = [s for s in jobs["image"]["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@")]
    assert [d["with"]["name"] for d in downloads] == ["dist"]
    join = next(s["run"] for s in jobs["manifest"]["steps"] if "imagetools create" in s.get("run", ""))
    assert '-t "$IMAGE:latest"' in join and "-amd64" in join and "-arm64" in join
    release = next(s["run"] for s in jobs["release"]["steps"] if "gh release create" in s.get("run", ""))
    assert "dist/pengumon-*" in release  # the wheel and the sdist are attached


def test_release_checks_the_tag_against_the_version_in_the_package_not_in_pyproject():
    steps = _load(WORKFLOWS / "release.yml")["jobs"]["image"]["steps"]
    check = next(s["run"] for s in steps if s.get("id") == "version")
    assert "src/pengumon/__init__.py" in check and "pyproject.toml" not in check


def test_ci_builds_the_package_once_and_tests_that_wheel_in_the_image_and_natively():
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    assert {"unit", "ui", "package", "container", "native-linux"} <= set(jobs)
    upload = next(s for s in jobs["package"]["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "dist"
    for name in ("container", "native-linux"):
        assert jobs[name]["needs"] == "package"
        download = next(s for s in jobs[name]["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@"))
        assert download["with"]["name"] == "dist"
    runners = {m["arch"]: m["os"] for m in jobs["container"]["strategy"]["matrix"]["include"]}
    assert runners == {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
    script = next(s["run"] for s in jobs["container"]["steps"] if "test-container.sh" in s.get("run", ""))
    assert "--prebuilt" in script
    native = "\n".join(s.get("run", "") for s in jobs["native-linux"]["steps"])
    assert "--require-hashes" in native and "pengumon run" in native and "/api/health" in native


def test_the_dockerfile_installs_the_wheel_and_sets_the_documented_environment():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG WHEEL_STAGE=wheel-built" in text  # scripts/deploy.sh builds from source with no build arguments
    assert "pip wheel --no-deps --no-build-isolation" in text
    assert "COPY src/" in text.split("FROM ${PYTHON_IMAGE} AS runtime")[0]  # the source is in the build stage only
    runtime = text.split("FROM ${PYTHON_IMAGE} AS runtime")[1]
    assert "COPY src" not in runtime and "-e ." not in runtime and "COPY tests" not in runtime
    assert "PENGUMON_DATA=/app/data PENGUMON_CONFIG=/app/config.yaml PENGUMON_HOST=0.0.0.0" in runtime
    assert 'CMD ["pengumon", "run"]' in runtime
    assert "USER 10001" in runtime


@pytest.fixture
def deploy_tree(tmp_path):
    """A copy of scripts/deploy.sh in a tree with a config.yaml, so it gets as far as the dependency gate."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "deploy.sh", tmp_path / "scripts" / "deploy.sh")
    (tmp_path / "config.yaml").write_text("{}\n")
    return tmp_path


def _run_gate(tree: Path, depguard_home: Path, **extra_env):
    env = {**os.environ, "DEPGUARD_HOME": str(depguard_home), **extra_env}
    env.pop("PENGUMON_REQUIRE_DEPGUARD", None)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(tree / "scripts" / "deploy.sh"), "--gate-only"],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, check=False,
    )


def _fake_depguard(directory: Path, exit_code: int) -> Path:
    directory.mkdir()
    script = directory / "depguard.sh"
    script.write_text(f'#!/bin/sh\necho "fake depguard $1"\nexit {exit_code}\n')
    script.chmod(0o755)
    return directory


def test_deploy_skips_the_gate_with_a_warning_when_depguard_is_missing(deploy_tree, tmp_path):
    res = _run_gate(deploy_tree, tmp_path / "nowhere")
    assert res.returncode == 0
    assert "dependency gate skipped: depguard not found" in res.stderr
    assert "hash-locked requirements still apply" in res.stderr


def test_deploy_refuses_when_depguard_is_missing_and_required(deploy_tree, tmp_path):
    res = _run_gate(deploy_tree, tmp_path / "nowhere", PENGUMON_REQUIRE_DEPGUARD="1")
    assert res.returncode == 1
    assert "Refusing to deploy" in res.stderr


def test_deploy_runs_check_and_audit_when_depguard_is_installed(deploy_tree, tmp_path):
    res = _run_gate(deploy_tree, _fake_depguard(tmp_path / "dg", 0))
    assert res.returncode == 0
    assert "fake depguard check" in res.stdout and "fake depguard audit" in res.stdout
    assert "skipped" not in res.stderr


def test_deploy_still_refuses_when_an_installed_depguard_fails(deploy_tree, tmp_path):
    res = _run_gate(deploy_tree, _fake_depguard(tmp_path / "dg", 1))
    assert res.returncode == 1
    assert "Dependency gate failed" in res.stderr
