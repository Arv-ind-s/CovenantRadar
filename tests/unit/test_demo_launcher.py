"""The judge launcher must never silently switch live AI to offline replay."""

import pytest

from scripts import demo_up


def test_launcher_reads_only_gemini_settings_and_process_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_up, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        'GEMINI_API_KEY="file-credential" # comment\n'
        "COVENANT_RADAR_AI__MODEL=gemini-3.8-flash\n"
        "COVENANT_RADAR_DATABASE__URL=sqlite:///some-other-database\n"
    )
    monkeypatch.setenv("GEMINI_API_KEY", "process-credential")
    monkeypatch.delenv("COVENANT_RADAR_AI__MODEL", raising=False)
    assert demo_up.gemini_environment() == {
        "GEMINI_API_KEY": "process-credential",
        "COVENANT_RADAR_AI__MODEL": "gemini-3.8-flash",
    }


def test_missing_key_stops_default_judge_launch_before_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(demo_up, "ROOT", tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["demo_up.py", "--check-only"])
    with pytest.raises(SystemExit) as result:
        demo_up.main()
    assert result.value.code == 2
    assert "Set GEMINI_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "var").exists()
