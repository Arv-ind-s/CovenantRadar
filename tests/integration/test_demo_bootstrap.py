"""Contract checks for the shared, isolated judge-demo bootstrap."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]


def test_demo_bootstrap_runs_the_required_steps_in_order() -> None:
    content = (ROOT / "scripts/demo_up.py").read_text()
    required_steps = (
        "run(*preflight)",
        'run("-m", "radarctl", "migrate", "upgrade")',
        'run("-m", "radarctl", "seed", "--reference-portfolio", "--signal-days", "1")',
        'run("create_user.py")',
        'run("-m", "radarctl", "seed", "--demo-covenants")',
        'run("-m", "radarctl", "job", "run", "nightly.pipeline")',
        'run("-m", "radarctl", "serve")',
    )
    positions = [content.index(step) for step in required_steps]
    assert positions == sorted(positions)
    assert 'tempfile.mkdtemp(prefix="covenant-radar-demo-")' in content
    assert '"COVENANT_RADAR_AI__PROVIDER": "recorded" if args.offline_ai else "gemini"' in content
    windows = (ROOT / "scripts/demo_up.ps1").read_text()
    assert 'ChildPath "demo_up.py"' in windows
    assert "& $PythonPath @arguments" in windows
    assert "exit $LASTEXITCODE" in windows


def test_demo_bootstrap_does_not_persist_generated_secrets() -> None:
    content = (ROOT / "scripts/demo_up.py").read_text()
    assert "secrets.token_bytes(32)" in content
    assert 'env[f"COVENANT_RADAR_SECURITY_{name}"]' in content
    assert "write_text" not in content
    assert "Set-Content" not in (ROOT / "scripts/demo_up.ps1").read_text()
