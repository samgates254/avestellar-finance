"""Structural guards for the GitHub Actions release gates.

A workflow file is code that runs with a cloud credential attached, and nothing
executes it locally, so mistakes surface only when someone pushes. These tests
parse the committed workflows and assert the properties the security model
depends on:

* least-privilege token and no ``pull_request_target`` trigger (no write access
  for a fork's pull request);
* no consumption of repository secrets at all — every gate must run on synthetic
  configuration;
* service containers are throwaway PostgreSQL instances with a health check, and
  never the development database;
* gate steps cannot silently pass (no ``continue-on-error``, no unpinned action
  reference);
* every JavaScript action runs on the Node.js 24 runtime GitHub now provides --
  each reference is floored at a version whose ``action.yml`` was verified to
  declare ``runs.using: node24``;
* the browser job calls the guarded lifecycle script rather than raw Playwright;
* the interpreter version comes from the repository pin (``.python-version``)
  instead of a second hardcoded value, and the PostgreSQL majors under test match
  the staging target.

Parsing only: this cannot prove a GitHub-hosted run will succeed, and it does not
pretend to. It proves the reviewed file is the file that will run.
"""

import re
from pathlib import Path

import pytest
import yaml

from tests.test_no_committed_credentials import (
    KNOWN_TOKEN_PREFIXES,
    LITERAL_SECRET_KEY,
    URL_WITH_PASSWORD,
    _is_placeholder,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
PYTHON_PIN = REPO_ROOT / ".python-version"

# A pin must name one exact release; "3.14" would let the patch float away.
EXACT_RELEASE = re.compile(r"\d+\.\d+\.\d+")
# Staging runs PostgreSQL 16; 18 is the next major the suite must survive.
STAGING_POSTGRES_MAJOR = "16"
BACKEND_POSTGRES_MAJORS = {"16", "18"}

# GitHub removed the Node.js 20 runtime from hosted runners, so every JavaScript
# action must execute on Node.js 24. Each floor below is the reference whose
# committed action.yml declares `runs.using: node24` -- read on the action's own
# tag, not assumed from the tag number. Newer majors are fine; going back below a
# floor, or adding an action nobody has inspected, is not.
NODE24_ACTION_FLOORS: dict[str, tuple[int, ...]] = {
    "actions/checkout": (7,),
    "actions/setup-python": (7,),
    "actions/setup-node": (7,),
    "actions/cache": (5,),
    "actions/upload-artifact": (7,),
    "pnpm/action-setup": (4, 4, 0),
}
# Opting out of the modern runtime instead of upgrading would hide the warning
# while leaving the workflow unsupported.
FORBIDDEN_RUNTIME_OVERRIDES = ("ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION",)
ACTION_REFERENCE = re.compile(r"^[\w.-]+/[\w.-]+@v\d+(?:\.\d+){0,2}$")
FLOATING_ACTION_REFS = {"main", "master", "latest", "head"}
DEV_DATABASE_NAMES = {"avestellar_finance", "postgres", "template1"}
E2E_DATABASE_PREFIX = "avestellar_live_http_"

REQUIRED_JOBS = {"backend", "frontend", "browser-e2e"}
FRONTEND_GATE_COMMANDS = (
    "pnpm install --frozen-lockfile",
    "pnpm run lint",
    "pnpm run typecheck",
    "pnpm run test",
    "pnpm run build",
)


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.y*ml"))


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    # PyYAML resolves the unquoted YAML 1.1 ``on`` key to the boolean True.
    triggers = document.get("on", document.get(True))
    assert triggers is not None, "Workflow declares no triggers"
    if isinstance(triggers, str):
        triggers = {triggers: {}}
    return triggers


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _step_text(step: dict) -> str:
    """Everything executable or referenced by a step, as one searchable string."""
    return " ".join(str(step.get(key, "")) for key in ("name", "run", "uses", "with", "env"))


def _release_tuple(reference: str) -> tuple[int, ...]:
    """``owner/repo@v4.4.0`` -> ``(4, 4, 0)``, comparable across majors."""
    version = reference.rsplit("@", 1)[1]
    assert version.startswith("v"), f"{reference!r} has no v-prefixed tag"
    return tuple(int(part) for part in version[1:].split("."))


def _all_step_text(workflow: dict) -> list[str]:
    return [
        _step_text(step)
        for job in workflow["jobs"].values()
        for step in _steps(job)
    ]


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
class TestWorkflowHygiene:
    def test_parses_as_a_mapping_with_jobs(self, path):
        document = _load(path)
        assert isinstance(document, dict)
        jobs = document.get("jobs")
        assert isinstance(jobs, dict) and jobs, f"{path.name} defines no jobs"

    def test_every_job_declares_a_runner_and_steps(self, path):
        for name, job in _load(path)["jobs"].items():
            assert job.get("runs-on"), f"{name} has no runs-on"
            steps = _steps(job)
            assert steps, f"{name} has no steps"
            for step in steps:
                assert step.get("run") or step.get("uses"), f"{name} has an empty step"

    def test_does_not_use_pull_request_target(self, path):
        triggers = _triggers(_load(path))
        assert "pull_request_target" not in triggers, (
            f"{path.name} must not use pull_request_target: it grants a fork "
            "write-capable tokens and repository secrets."
        )

    def test_token_is_read_only(self, path):
        permissions = _load(path).get("permissions")
        assert permissions == {"contents": "read"}, (
            f"{path.name} must declare `permissions: contents: read`"
        )

    def test_no_job_consumes_repository_secrets(self, path):
        text = path.read_text(encoding="utf-8")
        assert "${{ secrets." not in text, (
            f"{path.name} references repository secrets; release gates must run "
            "on synthetic configuration only."
        )

    def test_actions_are_pinned_to_release_tags(self, path):
        for step in (s for job in _load(path)["jobs"].values() for s in _steps(job)):
            reference = step.get("uses")
            if not reference:
                continue
            assert ACTION_REFERENCE.match(reference), (
                f"Action {reference!r} is not pinned to a version tag"
            )
            assert reference.rsplit("@", 1)[1] not in FLOATING_ACTION_REFS

    def test_javascript_actions_run_on_the_node24_runtime(self, path):
        for step in (s for job in _load(path)["jobs"].values() for s in _steps(job)):
            reference = step.get("uses")
            if not reference:
                continue
            action = reference.split("@", 1)[0]
            floor = NODE24_ACTION_FLOORS.get(action)
            assert floor is not None, (
                f"{action!r} has not been reviewed for its JavaScript runtime: "
                "read its action.yml, confirm `runs.using: node24`, then add the "
                "verified reference to NODE24_ACTION_FLOORS"
            )
            floor_tag = "v" + ".".join(str(part) for part in floor)
            assert _release_tuple(reference) >= floor, (
                f"{reference} predates the Node.js 24 runtime (floor {floor_tag}); "
                "hosted runners no longer provide Node.js 20"
            )

    def test_does_not_opt_out_of_the_action_runtime(self, path):
        text = path.read_text(encoding="utf-8")
        for override in FORBIDDEN_RUNTIME_OVERRIDES:
            assert override not in text, (
                f"{path.name} sets {override}: migrate the action instead of "
                "forcing it onto an unsupported runtime"
            )

    def test_gate_steps_cannot_swallow_failures(self, path):
        document = _load(path)
        for job_name, job in document["jobs"].items():
            assert job.get("continue-on-error") is not True, job_name
            for step in _steps(job):
                assert step.get("continue-on-error") is not True, (
                    f"{job_name}: step {step.get('name')!r} would ignore failures"
                )

    def test_no_step_loads_a_local_env_file(self, path):
        for text in _all_step_text(_load(path)):
            for pattern in ("source .env", "cp .env", "cat .env", ".env.example"):
                assert pattern not in text, (
                    f"{path.name} must not load a developer .env: found {pattern!r}"
                )

    def test_workflow_holds_no_real_credentials(self, path):
        """Apply the committed-secret patterns to the workflow itself.

        A workflow is a tracked configuration file like any other, so it is
        subject to ``test_no_committed_credentials``. That scan walks
        ``git ls-files`` and therefore cannot see a workflow until it is
        committed; this test closes the gap at authoring time.
        """
        text = path.read_text(encoding="utf-8")
        offenders = []
        for match in URL_WITH_PASSWORD.finditer(text):
            if not (
                _is_placeholder(match.group("user"))
                or _is_placeholder(match.group("password"))
            ):
                offenders.append(f"URL with credentials (user={match.group('user')!r})")
        for match in LITERAL_SECRET_KEY.finditer(text):
            value = match.group("value").strip().strip("'\"")
            if value and not _is_placeholder(value):
                offenders.append("literal SECRET_KEY")
        for match in KNOWN_TOKEN_PREFIXES.finditer(text):
            offenders.append(f"provider token prefix {match.group(0)[:6]}...")
        assert not offenders, f"{path.name} embeds real credentials:\n" + "\n".join(offenders)


@pytest.mark.skipif(not CI_WORKFLOW.is_file(), reason="ci.yml not present")
class TestCiWorkflow:
    @pytest.fixture(scope="class")
    def workflow(self) -> dict:
        return _load(CI_WORKFLOW)

    def test_defines_the_three_expected_jobs(self, workflow):
        assert REQUIRED_JOBS <= set(workflow["jobs"])

    def test_browser_job_depends_on_both_gate_jobs(self, workflow):
        needs = workflow["jobs"]["browser-e2e"].get("needs") or []
        assert {"backend", "frontend"} <= set(needs)

    def test_postgres_services_are_ephemeral_and_health_checked(self, workflow):
        for job_name in ("backend", "browser-e2e"):
            service = workflow["jobs"][job_name]["services"]["postgres"]
            assert service["image"].startswith("postgres:")
            options = " ".join(str(service["options"]).split())
            assert "--health-cmd" in options, f"{job_name} has no readiness probe"
            password = str(service["env"]["POSTGRES_PASSWORD"])
            assert _is_placeholder(password), (
                f"{job_name} service password must be a synthetic placeholder"
            )

    @pytest.mark.parametrize("job_name", ["backend", "browser-e2e"])
    def test_database_targets_never_reach_the_development_database(self, workflow, job_name):
        database_url = str(workflow["jobs"][job_name]["env"]["DATABASE_URL"])
        assert database_url.startswith("postgresql://")
        assert "127.0.0.1:5432" in database_url, "Service containers are reachable on localhost"
        name = database_url.rsplit("/", 1)[-1]
        assert name not in DEV_DATABASE_NAMES, f"{job_name} targets the development database"

    def test_browser_job_uses_a_prefixed_disposable_database(self, workflow):
        env = workflow["jobs"]["browser-e2e"]["env"]
        assert str(env["E2E_AV_DATABASE_NAME"]).startswith(E2E_DATABASE_PREFIX)
        # The safety prefix is the guard the runner itself re-asserts; it must not
        # be optional in CI.
        assert str(env.get("E2E_DROP_DB", "0")) in {"0", "1"}

    def test_synthetic_secret_key_is_not_a_real_credential(self, workflow):
        secret_key = str(workflow["env"]["SECRET_KEY"])
        assert _is_placeholder(secret_key), (
            "The workflow SECRET_KEY must be recognisably synthetic so the "
            "committed-secret scanner does not (and should not) flag it."
        )
        assert workflow["env"]["DJANGO_ENV"] == "test"

    def test_browser_job_runs_the_guarded_lifecycle_script(self, workflow):
        runs = [str(step.get("run", "")) for step in _steps(workflow["jobs"]["browser-e2e"])]
        assert any("scripts/run_browser_e2e.sh" in command for command in runs), (
            "The E2E job must reuse the guarded runner, not raw Playwright"
        )
        assert not any(
            "playwright test" in command for command in runs
        ), "Raw `playwright test` would skip the disposable-database guards"

    def test_setup_python_reads_the_repository_pin(self, workflow):
        for job_name in ("backend", "browser-e2e"):
            setups = [
                step
                for step in _steps(workflow["jobs"][job_name])
                if str(step.get("uses", "")).startswith("actions/setup-python@")
            ]
            assert setups, f"{job_name} never installs Python"
            for step in setups:
                inputs = step.get("with") or {}
                assert inputs.get("python-version-file") == ".python-version", (
                    f"{job_name} must select Python through the repository pin so "
                    "CI cannot drift from the verified runtime"
                )
                assert "python-version" not in inputs, (
                    f"{job_name} keeps a competing hardcoded python-version"
                )

    def test_backend_matrix_covers_staging_and_the_next_major(self, workflow):
        strategy = workflow["jobs"]["backend"]["strategy"]
        majors = {str(value) for value in strategy["matrix"]["postgres"]}
        assert majors == BACKEND_POSTGRES_MAJORS, (
            f"Backend matrix runs {sorted(majors)}; expected "
            f"{sorted(BACKEND_POSTGRES_MAJORS)}"
        )
        image = workflow["jobs"]["backend"]["services"]["postgres"]["image"]
        assert "${{ matrix.postgres }}" in image, (
            "The service image must follow the matrix, otherwise every leg tests "
            "the same server version"
        )

    def test_browser_job_matches_the_staging_postgres_major(self, workflow):
        image = workflow["jobs"]["browser-e2e"]["services"]["postgres"]["image"]
        assert image == f"postgres:{STAGING_POSTGRES_MAJOR}-alpine", (
            "The E2E journey must run the PostgreSQL major staging runs"
        )

    def test_browser_job_uses_the_runner_interpreter(self, workflow):
        text = " ".join(_all_step_text(workflow))
        assert "E2E_PYTHON" in text, (
            "The runner defaults to .venv/bin/python, which does not exist on a "
            "GitHub runner; CI must point E2E_PYTHON at the installed interpreter."
        )
        assert "/usr/bin/python3" not in text, (
            "The system interpreter has no project dependencies; resolve the "
            "setup-python interpreter instead."
        )

    def test_backend_job_runs_every_documented_gate(self, workflow):
        runs = " ".join(str(step.get("run", "")) for step in _steps(workflow["jobs"]["backend"]))
        for command in (
            "manage.py check",
            "makemigrations --check",
            "pytest",
            "spectacular --validate",
            "check --deploy",
        ):
            assert command in runs, f"Backend job is missing: {command}"
        for suite in (
            "test_settings_hardening.py",
            "test_cache_configuration.py",
            "test_auth_security.py",
            "test_daraja_callback.py",
            "test_no_committed_credentials.py",
            "test_dependency_pinning.py",
        ):
            assert suite in runs, f"Security suite not run in CI: {suite}"

    def test_frontend_job_runs_the_release_gates_in_order(self, workflow):
        runs = [
            " ".join(str(step.get("run", "")).split())
            for step in _steps(workflow["jobs"]["frontend"])
        ]
        runs = [command for command in runs if command]
        positions = []
        for expected in FRONTEND_GATE_COMMANDS:
            matches = [i for i, command in enumerate(runs) if expected in command]
            assert matches, f"Frontend job is missing: {expected}"
            positions.append(matches[0])
        assert positions == sorted(positions), "Frontend gates run out of order"


class TestRuntimePin:
    """The interpreter version must live in exactly one repository file."""

    @pytest.fixture(scope="class")
    def pinned(self) -> str:
        assert PYTHON_PIN.is_file(), (
            "No .python-version pin: CI would inherit whatever interpreter the "
            "runner image happens to default to."
        )
        lines = [
            line.strip()
            for line in PYTHON_PIN.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1, f"The pin must hold one version, found {lines}"
        return lines[0]

    def test_pins_an_exact_release(self, pinned):
        assert EXACT_RELEASE.fullmatch(pinned), (
            f"{pinned!r} is not an exact X.Y.Z release; a floating minor or patch "
            "defeats the point of a reproducible runtime"
        )

    def test_workflow_declares_no_competing_version_literal(self):
        text = CI_WORKFLOW.read_text(encoding="utf-8")
        assert "python-version:" not in text, (
            "ci.yml carries its own python-version; the pin is the single source "
            "of truth"
        )

    def test_documentation_names_the_pinned_runtime(self, pinned):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert pinned in readme, (
            f"README does not mention the pinned interpreter {pinned}"
        )
        gates = (REPO_ROOT / "docs" / "ci_release_gates.md").read_text(encoding="utf-8")
        assert ".python-version" in gates, (
            "docs/ci_release_gates.md must explain where the runtime comes from"
        )
