"""Repository guard: committed examples must not contain working credentials.

Milestone 7.1 removed a PostgreSQL URL with a working-looking local username and
password from the committed ``.env.example``. This test keeps it removed: it
scans the tracked configuration/example/documentation/script files for
credential-shaped strings and fails if a real-looking value reappears.

Deliberately out of scope
-------------------------
- ``tests/``: synthetic fixtures are required to look like credentials in order
  to exercise the redaction paths (and are asserted separately).
- Untracked files (``.env`` and anything else ignored by Git).

Placeholders are expected and allowed; a placeholder is not a secret.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Tracked files that are meant to be copied, read, or executed by operators.
SCANNED_SUFFIXES = {".md", ".sh", ".yml", ".yaml", ".example", ".ini", ".cfg", ".toml", ".env"}
SCANNED_NAMES = {".env.example", ".env.sample", ".env.template"}
# Large generated/third-party files carry no operator-editable configuration.
SKIP_PARTS = ("node_modules", "package-lock.json", "pnpm-lock.yaml", ".next", "staticfiles")
MAX_FILE_BYTES = 512 * 1024

URL_WITH_PASSWORD = re.compile(r"[a-z][a-z0-9+.-]*://(?P<user>[^/\s:@]+):(?P<password>[^/\s@]+)@", re.I)
# Both "KEY=value" (dotenv, shell) and "KEY: value" (YAML, CI workflow env
# blocks) assignments are covered, so a literal secret cannot hide in a workflow.
LITERAL_SECRET_KEY = re.compile(
    r"^\s*SECRET_KEY\s*[:=]\s*(?P<value>\S.*)$", re.MULTILINE
)
KNOWN_TOKEN_PREFIXES = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|sk_live_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)

# Values that are explicitly nonfunctional in a committed example.
PLACEHOLDER_VALUES = {
    "",
    "user",
    "username",
    "pass",
    "password",
    "passwd",
    "pwd",
    "secret",
    "changeme",
    "change-me",
    "example",
    "placeholder",
    "host",
    "database_name",
    "your-password",
    "your_password",
    "your-user",
    "your_user",
    "<user>",
    "<password>",
    "%(user)s",
    "%(password)s",
    "${db_user}",
    "${db_password}",
    "${database_url}",
    "${secret_key}",
}
PLACEHOLDER_TOKEN_MARKERS = (
    "replace-with",
    "change-me",
    "changeme",
    "your-",
    "your_",
    "my_",
    "dummy",
    "sample",
    # CI-only values: throwaway service-container credentials and the ephemeral
    # bootstrap secret in .github/workflows/ci.yml. They unlock nothing outside
    # the disposable runner, so they are nonfunctional rather than leaked.
    "synthetic",
    "not-a-real",
    "ci-only",
    "${",
    "<",
)


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return [name for name in result.stdout.split("\0") if name]


def _is_scanned(path: str) -> bool:
    if path.startswith("tests/"):
        return False
    if any(part in path for part in SKIP_PARTS):
        return False
    candidate = Path(path)
    return candidate.suffix in SCANNED_SUFFIXES or candidate.name in SCANNED_NAMES


def _scanned_documents() -> list[tuple[str, str]]:
    documents = []
    for path in _tracked_files():
        if not _is_scanned(path):
            continue
        full_path = REPO_ROOT / path
        try:
            if full_path.stat().st_size > MAX_FILE_BYTES:
                continue
            documents.append((path, full_path.read_text(encoding="utf-8", errors="replace")))
        except OSError:  # pragma: no cover - unreadable tracked file
            continue
    return documents


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in PLACEHOLDER_VALUES:
        return True
    return any(marker in lowered for marker in PLACEHOLDER_TOKEN_MARKERS)


def test_scanner_sees_the_example_configuration():
    """Guard against the scan silently matching nothing."""
    scanned = [path for path, _ in _scanned_documents()]
    assert ".env.example" in scanned


def test_no_working_credentials_in_committed_examples():
    offenders = []
    for path, content in _scanned_documents():
        for match in URL_WITH_PASSWORD.finditer(content):
            user = match.group("user")
            password = match.group("password")
            if _is_placeholder(user) or _is_placeholder(password):
                continue
            offenders.append(f"{path}: URL with credentials (user={user!r})")
    assert not offenders, (
        "Committed example/documentation files contain credential-shaped values:\n"
        + "\n".join(offenders)
    )


def test_no_literal_secret_keys_in_committed_examples():
    offenders = []
    for path, content in _scanned_documents():
        for match in LITERAL_SECRET_KEY.finditer(content):
            value = match.group("value").strip().strip("\"'")
            if not value or _is_placeholder(value):
                continue
            offenders.append(f"{path}: literal SECRET_KEY")
    assert not offenders, "Committed examples assign a literal SECRET_KEY:\n" + "\n".join(offenders)


def test_no_provider_api_tokens_in_committed_examples():
    offenders = []
    for path, content in _scanned_documents():
        for match in KNOWN_TOKEN_PREFIXES.finditer(content):
            offenders.append(f"{path}: {match.group(0)[:6]}...")
    assert not offenders, "Committed files contain provider token patterns:\n" + "\n".join(offenders)


def test_real_environment_files_are_not_tracked():
    tracked = set(_tracked_files())
    for name in (".env", ".env.local", ".env.staging", ".env.production"):
        assert name not in tracked, f"{name} must never be committed"

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"],
        cwd=str(REPO_ROOT),
    )
    # ``git check-ignore`` exits 0 when the path is ignored.
    assert ignored.returncode == 0, ".env must be ignored by Git"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("USER", True),
        ("PASSWORD", True),
        ("YOUR_LOCAL_USER", True),
        ("YOUR_LOCAL_PASSWORD", True),
        ("replace-with-a-unique-50-character-secret", True),
        ("${DATABASE_URL}", True),
        ("ci_synthetic_password", True),
        ("ci-only-synthetic-secret-0123456789abcdefghijklmnop", True),
        ("not-a-real-token", True),
        ("hunter2", False),
        ("s3cr3t-passw0rd", False),
        ("avestellar_ci_prod_password", False),
    ],
)
def test_placeholder_classification(value, expected):
    assert _is_placeholder(value) is expected
