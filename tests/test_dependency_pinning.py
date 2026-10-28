"""Dependency pinning and lockfile-drift guard.

CI installs with ``pip install -r requirements.txt`` and
``pnpm install --frozen-lockfile``. Both procedures are reproducible only when
every *direct* dependency carries an exact version and the committed lockfile
agrees with the manifest. These tests fail on a floating range, a duplicate
entry, or manifest/lockfile drift instead of letting a silent upstream upgrade
reach a release.

Scope is deliberately narrow: no network access, no package resolution, and no
transitive-dependency opinions. It checks the files an operator or CI trusts.
"""

import json
import re
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
FRONTEND = REPO_ROOT / "frontend"
PACKAGE_JSON = FRONTEND / "package.json"
LOCKFILE = FRONTEND / "pnpm-lock.yaml"
WORKSPACE_FILE = FRONTEND / "pnpm-workspace.yaml"

# name[extras]==version  — the only accepted shape for a direct requirement.
PINNED_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9,._-]+\])?"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9.+!_-]*)$"
)
# Exact semver for npm: no range operators, no tags, no protocol prefixes.
EXACT_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# Tooling the release gates themselves depend on.
REQUIRED_BACKEND_TOOLS = {"pytest", "pytest-django", "gunicorn", "whitenoise"}
REQUIRED_FRONTEND_SCRIPTS = {"lint", "typecheck", "test", "build"}


def _requirement_lines() -> list[tuple[int, str]]:
    lines = []
    for number, raw in enumerate(REQUIREMENTS.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((number, stripped))
    return lines


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _manifest() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


def _lockfile() -> dict:
    return yaml.safe_load(LOCKFILE.read_text(encoding="utf-8"))


def _is_git_ignored(path: Path) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(path.relative_to(REPO_ROOT))],
        cwd=str(REPO_ROOT),
    )
    return result.returncode == 0


class TestBackendPinning:
    def test_requirements_file_is_not_empty(self):
        assert _requirement_lines(), "requirements.txt has no requirements"

    def test_every_requirement_is_exactly_pinned(self):
        offenders = [
            f"line {number}: {text!r}"
            for number, text in _requirement_lines()
            if not PINNED_REQUIREMENT.match(text)
        ]
        assert not offenders, (
            "requirements.txt must pin every direct dependency with '==':\n"
            + "\n".join(offenders)
        )

    def test_no_duplicate_distributions(self):
        seen: dict[str, int] = {}
        duplicates = []
        for number, text in _requirement_lines():
            name = _normalized(PINNED_REQUIREMENT.match(text).group("name"))
            if name in seen:
                duplicates.append(f"{name} (lines {seen[name]} and {number})")
            seen[name] = number
        assert not duplicates, "Duplicate requirements:\n" + "\n".join(duplicates)

    def test_no_unpinned_sources_or_environment_markers(self):
        offenders = []
        for number, text in _requirement_lines():
            lowered = text.lower()
            if any(token in lowered for token in ("://", "git+", " @ ", "; ")):
                offenders.append(f"line {number}: {text!r}")
            elif text.startswith("-"):
                offenders.append(f"line {number}: {text!r}")
        assert not offenders, (
            "requirements.txt must not use URLs, VCS refs, or environment markers:\n"
            + "\n".join(offenders)
        )

    def test_ci_and_serving_tooling_is_pinned(self):
        pinned = {
            _normalized(PINNED_REQUIREMENT.match(text).group("name"))
            for _, text in _requirement_lines()
        }
        missing = REQUIRED_BACKEND_TOOLS - pinned
        assert not missing, f"Release tooling is not pinned: {sorted(missing)}"


class TestFrontendPinning:
    def test_manifest_uses_exact_versions(self):
        manifest = _manifest()
        offenders = []
        for section in ("dependencies", "devDependencies"):
            for name, version in manifest.get(section, {}).items():
                if not EXACT_SEMVER.match(str(version)):
                    offenders.append(f"{section}.{name}={version!r}")
        assert not offenders, (
            "frontend/package.json must use exact versions (no ^, ~, *, latest):\n"
            + "\n".join(offenders)
        )

    def test_release_scripts_exist(self):
        scripts = _manifest().get("scripts", {})
        missing = REQUIRED_FRONTEND_SCRIPTS - set(scripts)
        assert not missing, f"CI invokes missing npm scripts: {sorted(missing)}"

    def test_lockfile_is_committed_and_v9(self):
        assert LOCKFILE.is_file(), "frontend/pnpm-lock.yaml must be committed"
        assert _lockfile().get("lockfileVersion") == "9.0"
        assert WORKSPACE_FILE.is_file(), "frontend/pnpm-workspace.yaml is required"
        assert not _is_git_ignored(LOCKFILE), "The pnpm lockfile must not be git-ignored"
        assert not _is_git_ignored(PACKAGE_JSON), "package.json must not be git-ignored"

    def test_lockfile_specifiers_match_the_manifest(self):
        manifest = _manifest()
        importer = _lockfile()["importers"]["."]
        problems = []
        for section in ("dependencies", "devDependencies"):
            declared = manifest.get(section, {})
            locked = importer.get(section, {}) or {}
            for name, version in declared.items():
                entry = locked.get(name)
                if entry is None:
                    problems.append(f"{section}.{name} is missing from the lockfile")
                    continue
                specifier = str(entry.get("specifier"))
                if specifier != str(version):
                    problems.append(
                        f"{section}.{name}: manifest {version!r} != lockfile {specifier!r}"
                    )
            for name in set(locked) - set(declared):
                problems.append(f"{section}.{name} is in the lockfile but not the manifest")
        assert not problems, (
            "package.json and pnpm-lock.yaml have drifted (run `pnpm install`):\n"
            + "\n".join(problems)
        )
