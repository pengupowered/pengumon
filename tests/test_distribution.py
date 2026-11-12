"""Installing and releasing: the release compose file, the changelog, the GitHub workflows and the dependency gate in
scripts/deploy.sh. These files sit at the repository root, so Dockerfile.test copies them in."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pengumon import __version__
from platform_helpers import posix_shell

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


@posix_shell
def test_changelog_has_unreleased_and_a_section_for_the_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    notes = subprocess.run(
        [str(ROOT / "scripts" / "changelog-section.sh"), __version__], capture_output=True, text=True, check=False
    )
    assert notes.returncode == 0, notes.stderr
    assert notes.stdout.strip()
    assert "]: https://" not in notes.stdout  # the link references at the bottom are not release notes


@posix_shell
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
            # owner/repo[/sub-action]@<40 hex>, with the release it is in a comment (actions/cache/restore@...)
            assert re.fullmatch(r"[\w.-]+/[\w.-]+(/[\w.-]+)*@[0-9a-f]{40}", uses), f"{uses} is not pinned to a commit SHA"
    for job in workflow["jobs"].values():
        if "uses" in job:  # a reusable workflow of this repository
            assert job["uses"].startswith("./.github/workflows/")


def test_ci_runs_on_ci_branches_and_pull_requests_only_so_a_release_commit_is_tested_once():
    """The release commit is tested on its ci/<name> branch, then fast-forwarded to main: no second run on main, and
    release.yml does not call ci.yml (it finds the run that already passed)."""
    triggers = _load(WORKFLOWS / "ci.yml")["on"]
    assert set(triggers) == {"push", "pull_request"}  # no workflow_call, no schedule
    assert triggers["push"]["branches"] == ["ci/**"]  # not main, not tags


def test_ci_keeps_the_artifacts_a_release_downloads_long_enough():
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    uploads = {s["with"]["name"]: s["with"] for s in _steps({"jobs": jobs}) if str(s.get("uses", "")).startswith("actions/upload-artifact@")}
    assert set(uploads) == {"dist", "release-assets"}
    for name, w in uploads.items():
        assert w["retention-days"] >= 30, name


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflow_actions_run_on_node24_releases(name):
    """The node20 releases made every job warn ("Node.js 20 is deprecated"); these commits are node24 releases.
    A new pin needs its version comment and an entry here, checked against its action.yml at that commit."""
    node24 = {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1": "v7.0.1",
        "actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9": "v6.1.0",
        "actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9": "v6.1.0",
        "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9": "v7.0.2",
        "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333": "v8.0.2",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97": "v7.0.0",
        "docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069": "v4.4.1",
        "docker/login-action@dbcb813823bdd20940b903addbd779551569679f": "v4.6.0",
        "docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc": "v7.4.0",
        # actions/attest-build-provenance 4.2.2 wraps actions/attest 4.2.1 (node24); both released more than 14 days ago
        "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8": "v4.2.2",
        # a composite action that runs the publisher in a container (no node); v1.14.3 is too new to use yet
        "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33": "v1.14.2",
    }
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    for uses, comment in re.findall(r"uses: ([\w./-]+@[0-9a-f]{40}) # (\S+)", text):
        assert node24.get(uses) == comment, f"{uses} # {comment} is not a known node24 release"
    assert len(re.findall(r"uses: [\w./-]+@", text)) == len(re.findall(r"uses: [\w./-]+@[0-9a-f]{40} # v", text))


def test_one_job_saves_the_browser_runner_image_and_the_others_restore_it():
    """Two jobs saving one cache key collide (a 409 warning); a restore that may miss warns too."""
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    savers = [n for n, j in jobs.items() for s in j.get("steps", []) if str(s.get("uses", "")).startswith("actions/cache/save@")]
    assert savers == ["runner-image"]
    assert not any(str(s.get("uses", "")).startswith("actions/cache@") for j in jobs.values() for s in j.get("steps", []))
    for name, job in jobs.items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/cache/restore@") and name != "runner-image":
                assert step["with"].get("fail-on-cache-miss") is True, name
                assert "runner-image" in job["needs"], name


def test_release_does_not_run_the_tests_but_starts_by_finding_the_ci_run_of_this_commit():
    workflow = _load(WORKFLOWS / "release.yml")
    jobs = workflow["jobs"]
    assert list(jobs)[0] == "tested" and jobs["tested"]["name"] == "Tested commit"
    assert jobs["tested"].get("needs") is None  # the first job
    assert jobs["tested"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["tested"]["outputs"] == {"run-id": "${{ steps.ci.outputs.run-id }}"}
    find = next(s for s in jobs["tested"]["steps"] if s.get("id") == "ci")
    assert "scripts/find-tested-run.sh" in find["run"] and '"$GITHUB_SHA"' in find["run"]
    # no test job, no call of ci.yml
    assert not any("uses" in job for job in jobs.values())
    assert "ci.yml" not in yaml.safe_dump({n: j for n, j in jobs.items()})
    assert "scripts/test" not in (WORKFLOWS / "release.yml").read_text(encoding="utf-8")


def test_find_tested_run_asks_for_the_successful_push_run_of_ci_yml_for_exactly_this_commit():
    text = (ROOT / "scripts" / "find-tested-run.sh").read_text(encoding="utf-8")
    assert "actions/runs?head_sha=$sha&status=success&event=push" in text
    assert ".github/workflows/ci.yml" in text and '.event == "push"' in text and '.head_sha == "' in text
    assert "no successful CI run for $sha: push it to a ci/ branch first" in text and "exit 1" in text


def test_release_downloads_every_artifact_from_the_tested_ci_run_instead_of_rebuilding():
    jobs = _load(WORKFLOWS / "release.yml")["jobs"]
    downloads = {n: [s for s in j["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@")]
                 for n, j in jobs.items()}
    assert {n for n, d in downloads.items() if d} == {"image", "attest-files", "release", "pypi"}
    for name, steps in downloads.items():
        for step in steps:
            assert step["with"]["run-id"] == "${{ needs.tested.outputs.run-id }}", name
            assert step["with"]["github-token"] == "${{ secrets.GITHUB_TOKEN }}", name
            assert "tested" in jobs[name]["needs"] or jobs[name]["needs"] == "tested", name
            assert jobs[name]["permissions"]["actions"] == "read", name  # reading another run's artifacts
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "upload-artifact" not in text and "podman build" not in text  # no rebuild of the wheel or the files


def test_release_runs_in_order_and_only_publishes_with_the_permissions_it_needs():
    workflow = _load(WORKFLOWS / "release.yml")
    assert workflow["on"]["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]
    jobs = workflow["jobs"]
    assert jobs["image"]["needs"] == "tested"
    assert jobs["image"]["permissions"] == {"contents": "read", "packages": "write", "actions": "read"}
    assert jobs["manifest"]["needs"] == "image"
    assert jobs["manifest"]["permissions"] == {"contents": "read", "packages": "write"}
    assert jobs["release"]["needs"] == ["tested", "manifest", "attest-image", "attest-files", "publish-image"]
    assert jobs["release"]["permissions"] == {"contents": "write", "actions": "read"}


def test_only_the_publishing_and_attesting_jobs_may_ask_for_an_identity_token():
    """id-token: write lets a job prove who it is to PyPI and Sigstore; nothing else gets it."""
    jobs = _load(WORKFLOWS / "release.yml")["jobs"]
    holders = {name for name, job in jobs.items() if (job.get("permissions") or {}).get("id-token") == "write"}
    assert holders == {"pypi", "attest-image", "attest-files"}
    # nothing else but reading the tested run's artifacts: not even the repository contents
    assert jobs["pypi"]["permissions"] == {"id-token": "write", "actions": "read"}
    assert jobs["attest-files"]["permissions"] == {
        "contents": "read", "actions": "read", "id-token": "write", "attestations": "write"}
    assert jobs["attest-image"]["permissions"] == {
        "contents": "read", "packages": "write", "id-token": "write", "attestations": "write"}
    assert "id-token" not in (_load(WORKFLOWS / "release.yml")["permissions"])
    assert "id-token" not in str(_load(WORKFLOWS / "ci.yml"))


def test_pypi_publishing_waits_for_approval_comes_last_and_publishes_the_tested_wheel_and_sdist():
    jobs = _load(WORKFLOWS / "release.yml")["jobs"]
    pypi = jobs["pypi"]
    assert pypi["environment"]["name"] == "pypi"  # the GitHub environment with the required reviewer
    assert pypi["needs"] == ["tested", "release"]  # release last of the others: a version on PyPI cannot be withdrawn, so everything else came first
    names = [s["with"]["name"] for s in pypi["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@")]
    assert names == ["dist"]  # the wheel and sdist of the tested run, not the install scripts or the lock
    publish = next(s for s in pypi["steps"] if str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish@"))
    assert publish["with"] == {"packages-dir": "dist"}  # no password or token anywhere: trusted publishing
    assert "secrets." not in str(publish)
    assert str(pypi).count("secrets.") == str(pypi).count("secrets.GITHUB_TOKEN")  # only the token that reads the artifacts
    assert not any(job.get("environment") for name, job in jobs.items() if name != "pypi")


def test_release_attests_the_files_and_the_multi_arch_image_by_digest():
    jobs = _load(WORKFLOWS / "release.yml")["jobs"]
    files = next(s for s in jobs["attest-files"]["steps"] if str(s.get("uses", "")).startswith("actions/attest-build-provenance@"))
    assert files["with"] == {"subject-path": "release-assets/*"}
    image = next(s for s in jobs["attest-image"]["steps"] if str(s.get("uses", "")).startswith("actions/attest-build-provenance@"))
    assert image["with"]["subject-name"] == "ghcr.io/pengupowered/pengumon"
    assert image["with"]["subject-digest"] == "${{ needs.manifest.outputs.digest }}"
    assert image["with"]["push-to-registry"] is True
    assert jobs["attest-image"]["needs"] == "manifest" and jobs["manifest"]["outputs"]["digest"]


def test_release_attaches_the_files_the_install_jobs_installed_from():
    jobs = _load(WORKFLOWS / "release.yml")["jobs"]
    steps = jobs["release"]["steps"]
    assert [s["with"]["name"] for s in steps if str(s.get("uses", "")).startswith("actions/download-artifact@")] == ["release-assets"]
    create = next(s["run"] for s in steps if "gh release create" in s.get("run", ""))
    assert "release-assets/*" in create and "sha256sum --check SHA256SUMS" in create
    ci_jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    upload = next(s for s in ci_jobs["release-assets"]["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "release-assets"


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
    assert "-candidate" in join and "-amd64" in join and "-arm64" in join  # latest comes later (publish-image)
    release = next(s["run"] for s in jobs["release"]["steps"] if "gh release create" in s.get("run", ""))
    assert "release-assets/*" in release  # the wheel, the sdist, the lock, the install scripts and SHA256SUMS


def test_release_checks_the_tag_against_the_version_in_the_package_not_in_pyproject():
    steps = _load(WORKFLOWS / "release.yml")["jobs"]["image"]["steps"]
    check = next(s["run"] for s in steps if s.get("id") == "version")
    assert "src/pengumon/__init__.py" in check and "pyproject.toml" not in check


def test_ci_builds_the_package_once_and_tests_that_wheel_in_the_image_and_natively():
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    assert {"unit", "ui", "package", "container", "native", "netns-trace", "internet-trace", "release-assets",
            "install"} <= set(jobs)
    upload = next(s for s in jobs["package"]["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "dist"
    for name in ("container", "native"):
        assert "package" in jobs[name]["needs"]
        download = next(s for s in jobs[name]["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@"))
        assert download["with"]["name"] == "dist"
    runners = {m["arch"]: m["os"] for m in jobs["container"]["strategy"]["matrix"]["include"]}
    assert runners == {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
    test = next(s for s in jobs["container"]["steps"] if "test-container.sh" in s.get("run", ""))
    assert "--prebuilt" in test["run"]
    assert "if" not in test  # every architecture builds and tests the image; only the browser check is amd64-only
    assert "--no-e2e" in test["run"]
    # Linux x64 is the unit job and the image job; the native legs are the other systems
    assert jobs["native"]["strategy"]["matrix"]["os"] == ["ubuntu-24.04-arm", "macos-latest", "windows-latest"]
    setup = next(s for s in jobs["native"]["steps"] if str(s.get("uses", "")).startswith("actions/setup-python@"))
    assert setup["with"]["cache"] == "pip" and "requirements-dev.txt" in setup["with"]["cache-dependency-path"]
    assert jobs["native"]["env"]["PENGUMON_REQUIRE_ICMP"] == "1"
    native = "\n".join(s.get("run", "") for s in jobs["native"]["steps"])
    assert "--require-hashes" in native and "pengumon run" in native and "/api/health" in native
    assert "pengumon doctor" in native and "pytest" in native
    assert jobs["internet-trace"]["continue-on-error"] is True


def test_every_pytest_run_in_ci_reports_failures_as_annotations():
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    for name in ("unit", "ui", "native"):
        steps = jobs[name]["steps"]
        annotate = [s for s in steps if "junit-annotations.py" in s.get("run", "")]
        assert annotate and annotate[0]["if"] == "always()", name
        runs = "\n".join(s.get("run", "") for s in steps)
        assert "PENGUMON_JUNIT_DIR" in runs or "--junitxml" in runs, name


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
    if sys.platform == "win32":
        pytest.skip("scripts/deploy.sh is the maintainer's bash deploy on Linux")
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


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_no_step_only_function_outside_steps(name):
    """hashFiles() is evaluated only inside steps; in the workflow's or a job's env GitHub rejects the whole file
    (a run with no jobs at all)."""
    workflow = _load(WORKFLOWS / name)
    assert "hashFiles" not in str(workflow.get("env", {}))
    for job in workflow["jobs"].values():
        assert "hashFiles" not in str(job.get("env", {})) and "hashFiles" not in str(job.get("strategy", {}))


def test_the_install_job_runs_the_install_scripts_from_the_release_files_on_three_systems_and_uninstalls():
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    install = jobs["install"]
    assert install["needs"] == "release-assets"
    assert install["strategy"]["matrix"]["os"] == ["ubuntu-24.04", "macos-latest", "windows-latest"]
    assert install["strategy"]["fail-fast"] is False
    download = next(s for s in install["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@"))
    assert download["with"]["name"] == "release-assets"
    runs = "\n".join(s.get("run", "") for s in install["steps"])
    assert "install.sh --from release-assets --yes --no-browser" in runs
    assert "install.ps1 -From release-assets -NoBrowser" in runs
    assert "install.sh --uninstall --yes" in runs and "install.ps1 -Uninstall" in runs
    for phase in ("installed", "upgraded", "removed"):
        assert f"ci-install-check.sh {phase}" in runs
    diagnose = next(s for s in install["steps"] if "ci-install-check.sh diagnose" in s.get("run", ""))
    assert diagnose["if"] == "failure()"
    assert runs.index("ci-install-check.sh installed") < runs.index("ci-install-check.sh upgraded") < runs.index(
        "ci-install-check.sh removed")


def test_the_image_takes_the_wheel_by_copy_so_the_build_cache_keys_on_its_content():
    # A RUN --mount=type=bind layer is cached on the command text alone: Podman reused a layer holding another
    # tree's wheel (reproduced: two trees differing by one line, built one after the other, the second image held the
    # first tree's code). COPY --from keys the cache on the wheel's content.
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--mount=type=bind,from=wheel" not in dockerfile
    assert "COPY --from=wheel" in dockerfile


def test_deploy_and_the_container_test_check_the_image_holds_this_trees_code():
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    container_test = (ROOT / "scripts" / "test-container.sh").read_text(encoding="utf-8")
    assert "scripts/image-matches-tree.sh" in deploy and "fail " in deploy.split("image-matches-tree.sh", 1)[1][:200]
    assert "scripts/image-matches-tree.sh" in container_test


def test_every_release_job_that_writes_to_the_registry_logs_in_first():
    # v0.20.0's release failed at "Image provenance": push-to-registry needs credentials for ghcr.io in that job too.
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))["jobs"]
    for name, job in jobs.items():
        steps = job.get("steps", [])
        text = yaml.safe_dump(steps)
        writes = "push-to-registry: true" in text or "push: true" in text or "imagetools create" in text
        if not writes:
            continue
        uses = [s.get("uses", "") for s in steps]
        login = next((i for i, u in enumerate(uses) if u.startswith("docker/login-action@")), None)
        first_write = next(i for i, s in enumerate(steps)
                           if any(k in yaml.safe_dump(s) for k in ("push-to-registry: true", "push: true", "imagetools create")))
        assert login is not None and login < first_write, f"job {name} writes to the registry without logging in first"


def test_each_provenance_step_is_retried_once_after_a_transient_failure():
    # v0.20.2's release stopped at "Image provenance" on a transient Sigstore error ("error fetching tlog entry").
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))["jobs"]
    attest_jobs = {n: j for n, j in jobs.items() if any("attest-build-provenance" in s.get("uses", "") for s in j["steps"])}
    assert set(attest_jobs) == {"attest-image", "attest-files"}
    for name, job in attest_jobs.items():
        steps = [s for s in job["steps"] if "attest-build-provenance" in s.get("uses", "")]
        assert len(steps) == 2, name
        first, second = steps
        assert first.get("id") == "attest" and first.get("continue-on-error") is True, name
        assert second.get("if") == "steps.attest.outcome == 'failure'" and "continue-on-error" not in second, name
        assert first["with"] == second["with"], name


@pytest.mark.parametrize("name", ["docker-compose.yml", "compose.release.yaml"])
def test_both_compose_files_drop_every_capability_but_net_raw_and_limit_memory_and_processes(name):
    service = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["services"]["pengumon"]
    assert service["cap_drop"] == ["ALL"] and service["cap_add"] == ["NET_RAW"]
    assert service["mem_limit"] == "512m" and service["pids_limit"] == 256
    # no-new-privileges would disable the file capability the image's Python relies on
    assert "no-new-privileges" not in str(service.get("security_opt", []))


# --- A second deploy in the same moment cannot overwrite the first one's rollback point ---------------------------

def _dry_run_deploy(tree: Path, tmp_path: Path, existing_tags: str = ""):
    """deploy.sh --dry-run with a fake podman (`image exists` is true for localhost/pengumon:latest and `existing_tags`)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    podman = bin_dir / "podman"
    podman.write_text(
        '#!/bin/sh\n'
        'if [ "$1 $2" = "image exists" ]; then\n'
        f'  case "$3" in localhost/pengumon:latest) exit 0 ;; {existing_tags or "NONE"}) exit 0 ;; esac\n'
        '  exit 1\nfi\n'
        'exit 1\n')
    podman.chmod(0o755)
    home = tmp_path / "home"
    (home / "backups").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "DEPGUARD_HOME": str(tmp_path / "nowhere"),
           "PENGUMON_BACKUPS": str(home / "backups")}
    env.pop("PENGUMON_REQUIRE_DEPGUARD", None)
    return subprocess.run(["bash", str(tree / "scripts" / "deploy.sh"), "--dry-run", "--no-build"],
                          capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, check=False)


def test_deploy_stamps_to_the_second(deploy_tree, tmp_path):
    res = _dry_run_deploy(deploy_tree, tmp_path)
    assert res.returncode == 0, res.stderr
    assert re.search(r"pre-deploy-\d{4}-\d{2}-\d{2}-\d{6}\b", res.stdout), res.stdout


def test_deploy_refuses_to_reuse_a_rollback_tag(deploy_tree, tmp_path):
    # every second from now on counts as taken (the tag glob matches any stamp)
    res = _dry_run_deploy(deploy_tree, tmp_path, existing_tags='"localhost/pengumon:pre-deploy-"*')
    assert "rollback point" in res.stderr and "already exists" in res.stderr
    assert "DRYRUN: podman tag" not in res.stdout


def test_deploy_refuses_to_reuse_a_backup_folder(deploy_tree, tmp_path):
    home = tmp_path / "home" / "backups"
    home.mkdir(parents=True)
    # a fake `date` pins the stamp, and the folder for that stamp already exists
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    date = fake_bin / "date"
    date.write_text('#!/bin/sh\ncase "$*" in *%Y-%m-%d-%H%M%S*) echo 2026-01-02-030405 ;; *) exec /bin/date "$@" ;; esac\n')
    date.chmod(0o755)
    (home / "2026-01-02-030405").mkdir()
    res = _dry_run_deploy(deploy_tree, tmp_path)
    assert res.returncode == 1 and "already exists" in res.stderr


def test_deploy_removes_only_folders_it_made_after_a_good_deploy():
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "-regex" in deploy and "umask 077" in deploy


def test_the_image_is_tagged_with_the_version_and_latest_only_after_its_attestation_succeeded():
    # The tags used to be pushed by the job before the attestation job, so a failed attestation left them public.
    jobs = _load(WORKFLOWS / "release.yml")["jobs"]
    for name, job in jobs.items():
        for step in job["steps"]:
            run = step.get("run", "")
            if "imagetools create" in run:
                tags = re.findall(r'-t "\$IMAGE:([^"]+)"', run)
                if name == "publish-image":
                    assert tags == ["$version", "latest"] and "attest-image" in job["needs"], name
                else:
                    assert tags == ["$version-candidate"] and "latest" not in run, name
    publish = jobs["publish-image"]
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert any(str(s.get("uses", "")).startswith("docker/login-action@") for s in publish["steps"])
    assert "publish-image" in jobs["release"]["needs"] and "attest-image" in jobs["release"]["needs"]
