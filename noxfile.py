"""Nox sessions used by the Covenant Radar quality gate."""

from __future__ import annotations

from pathlib import Path

import nox

PYTHON_VERSION = "3.12"
ROOT = Path(__file__).parent
LOCK_FILE = ROOT / "requirements.lock"


def _install(session: nox.Session, *packages: str) -> None:
    """Install a narrowly scoped, lock-constrained tool set."""
    if not LOCK_FILE.is_file():
        session.error("requirements.lock is required to run quality sessions")
    session.install("--constraint", str(LOCK_FILE), *packages)


def _skip(session: nox.Session, step: str, task: str) -> None:
    """Report an intentionally unavailable future gate step."""
    session.log(f"SKIP {step} — not yet implemented ({task})")


def _install_application(session: nox.Session) -> None:
    """Install the application and its test tools in each isolated session."""
    _install(session, ".[dev]")


@nox.session(name="format", python=PYTHON_VERSION)
def format_check(session: nox.Session) -> None:
    """Check formatting without modifying working files."""
    _install(session, "ruff")
    session.run("ruff", "format", "--check", "src", "tests", "noxfile.py")


@nox.session(name="lint", python=PYTHON_VERSION)
def lint(session: nox.Session) -> None:
    """Run static lint checks."""
    _install(session, "ruff")
    session.run("ruff", "check", "src", "tests", "noxfile.py")


@nox.session(name="types", python=PYTHON_VERSION)
def types(session: nox.Session) -> None:
    """Run the project's type checker."""
    _install_application(session)
    session.run("mypy", "src")


@nox.session(name="imports", python=PYTHON_VERSION)
def imports(session: nox.Session) -> None:
    """Enforce architectural import contracts."""
    _install(session, "import-linter")
    session.install("--no-deps", "--editable", ".")
    session.run("lint-imports")


@nox.session(name="tests", python=PYTHON_VERSION)
def tests(session: nox.Session) -> None:
    """Run the unit and available property tests."""
    _install_application(session)
    test_paths = ["tests/unit"]
    if (ROOT / "tests" / "property").is_dir():
        test_paths.append("tests/property")
    else:
        _skip(session, "property-tests", "T-027")
    session.run("pytest", *test_paths)


@nox.session(name="alembic_drift", python=PYTHON_VERSION)
def alembic_drift(session: nox.Session) -> None:
    """Check migration drift when migrations have been implemented."""
    if not (ROOT / "alembic.ini").is_file():
        _skip(session, "alembic-drift", "T-010")
        return
    _install_application(session)
    session.run("alembic", "check")


@nox.session(name="integration", python=PYTHON_VERSION)
def integration(session: nox.Session) -> None:
    """Run integration tests against the required PostgreSQL service."""
    _install_application(session)
    session.run("pytest", "-q", "tests/integration")


@nox.session(name="migration", python=PYTHON_VERSION)
def migration(session: nox.Session) -> None:
    """Run migration tests: dual-engine upgrade/downgrade, drift, and
    rollback proofs (`T-010`). The PostgreSQL half requires the same
    service `integration` does."""
    _install_application(session)
    session.run("pytest", "-q", "tests/migration")


@nox.session(name="contract", python=PYTHON_VERSION)
def contract(session: nox.Session) -> None:
    _install_application(session)
    session.run("pytest", "-q", "tests/contract")


@nox.session(name="seed", python=PYTHON_VERSION)
def seed(session: nox.Session) -> None:
    _skip(session, "seed-determinism", "T-011")


@nox.session(name="evaluation", python=PYTHON_VERSION)
def evaluation(session: nox.Session) -> None:
    """Run the offline two-arm evaluation with the checked-in score floors."""
    if not LOCK_FILE.is_file():
        session.error("requirements.lock is required to run quality sessions")
    session.install("--constraint", str(LOCK_FILE), str(ROOT))
    session.run(
        "python",
        "-m",
        "evaluation.run",
        "--both-arms",
        "--gate",
        "--floors",
        "evaluation/floors.json",
    )


@nox.session(name="e2e", python=PYTHON_VERSION)
def e2e(session: nox.Session) -> None:
    _install_application(session)
    session.run("python", "-m", "playwright", "install", "--with-deps", "chromium")
    session.run("pytest", "-q", "tests/e2e")


@nox.session(name="a11y", python=PYTHON_VERSION)
def a11y(session: nox.Session) -> None:
    _install_application(session)
    session.run("pytest", "-q", "tests/a11y")


@nox.session(name="security", python=PYTHON_VERSION)
def security(session: nox.Session) -> None:
    _skip(session, "security-scanning", "T-019")


@nox.session(name="performance", python=PYTHON_VERSION)
def performance(session: nox.Session) -> None:
    _skip(session, "performance", "T-160")
