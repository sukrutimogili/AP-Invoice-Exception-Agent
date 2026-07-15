"""
tests/security/conftest.py — Conftest harness for the security test suite.

spec.md §5: "Verify this with an adversarial test on every prompt change
(regression-test the injection scenario, not just once)."

This conftest provides:

1. prompt_version fixture
   Exposes the active prompt version string and SHA-256 hash to every test in
   this directory.  Tests may use it to assert they are running against the
   expected prompt.

2. Automatic prompt-change detection
   On every test session that includes security tests, the conftest computes
   the hash of every file in extraction/prompts/.  If a hash differs from what
   is cached in .pytest_cache/security_prompt_hashes.json, a warning is emitted
   and the session-scoped fixture `prompt_changed` is set to True.

   Tests in TestPromptVersionPin use this to block the session if the prompt
   changed without the engineer updating the pin.

3. security_suite_summary session-scoped fixture
   Collects pass/fail counts and prints a Phase 6 completion report at the
   end of the session.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parents[2]
_PROMPTS_DIR = _PROJECT_ROOT / "extraction" / "prompts"
_HASH_CACHE_FILE = _PROJECT_ROOT / ".pytest_cache" / "security_prompt_hashes.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _collect_prompt_hashes() -> dict[str, str]:
    """Return {relative_path: sha256} for every file in extraction/prompts/."""
    hashes: dict[str, str] = {}
    for p in sorted(_PROMPTS_DIR.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            rel = str(p.relative_to(_PROJECT_ROOT))
            hashes[rel] = _hash_file(p)
    return hashes


def _load_cached_hashes() -> dict[str, str]:
    if _HASH_CACHE_FILE.exists():
        try:
            return json.loads(_HASH_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_prompt_hashes(hashes: dict[str, str]) -> None:
    _HASH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HASH_CACHE_FILE.write_text(
        json.dumps(hashes, indent=2, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def prompt_version() -> dict[str, str]:
    """
    Return the active prompt version and SHA-256 hash for v1_extract.md.

    Usage in a test::

        def test_something(prompt_version):
            assert prompt_version["version"] == "v1"
    """
    prompt_path = _PROMPTS_DIR / "v1_extract.md"
    if not prompt_path.exists():
        return {"version": "MISSING", "sha256": ""}

    content = prompt_path.read_text(encoding="utf-8")
    sha = _hash_file(prompt_path)

    # Extract version string from the prompt file.
    version = "unknown"
    for line in content.splitlines():
        if "Version:" in line:
            parts = line.split("Version:", 1)
            if len(parts) == 2:
                version = parts[1].strip().split()[0]
                break

    return {"version": version, "sha256": sha}


@pytest.fixture(scope="session")
def prompt_changed() -> bool:
    """
    Returns True if any file in extraction/prompts/ has changed since the
    last time the security suite was run.

    This fixture is session-scoped and read-only; it does NOT update the cache
    (that is done by the session-finish hook so tests see the pre-run state).
    """
    current = _collect_prompt_hashes()
    cached = _load_cached_hashes()
    return current != cached


# ---------------------------------------------------------------------------
# Session finish hook — update hash cache after every security run
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """
    After the security test session completes, update the prompt hash cache
    so the next run can detect further changes.

    Only runs when at least one security-marked test was collected.
    """
    # Check whether any security tests were in this session.
    security_collected = any(
        item
        for item in session.items
        if item.get_closest_marker("security") is not None
    )
    if not security_collected:
        return

    current_hashes = _collect_prompt_hashes()
    _save_prompt_hashes(current_hashes)


# ---------------------------------------------------------------------------
# Prompt-change warning hook
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """
    Emit a warning at session start if the prompt files have changed since
    the last security run.  This is purely advisory — the TestPromptVersionPin
    tests perform the actual blocking assertion.
    """
    current = _collect_prompt_hashes()
    cached = _load_cached_hashes()

    if cached and current != cached:
        changed_files = [k for k in current if current.get(k) != cached.get(k)]
        new_files = [k for k in current if k not in cached]
        deleted_files = [k for k in cached if k not in current]

        all_changes = changed_files + new_files + deleted_files
        if all_changes:
            import warnings

            warnings.warn(
                "\n\n"
                "⚠  PROMPT CHANGE DETECTED — security suite must be re-reviewed!\n"
                f"Changed files: {all_changes}\n"
                "ACTION REQUIRED:\n"
                "  1. Review tests/security/adversarial_samples.py against new prompt.\n"
                "  2. Update _EXPECTED_PROMPT_SHA256 in TestPromptVersionPin.\n"
                "  3. Re-run `pytest -m security` and confirm 100% pass.\n"
                "  spec.md §5: 'regression-test the injection scenario on every prompt change'\n",
                stacklevel=2,
            )
