"""doctor 的配置路径必须与全项目同一真相源。

背景：合并 #70/#71 后 web/app.py 与 easel/local_agents.py 都已统一走
easel.openclaw_workspace.config_path()，doctor._openclaw_config_path() 仍自己
解析 EASEL_OPENCLAW_STATE_DIR。两处语义有细微差别——环境变量是空串时
doctor 返回 Path("")/"openclaw.json"（相对路径，随当前工作目录漂移），
config_path() 会正确回落默认 profile 目录。统一委托后由本文件钉住。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from easel import openclaw_workspace as ws


def test_doctor_delegates_to_config_path(tmp_path, monkeypatch):
    import easel.commands.doctor as doctor

    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    assert doctor._openclaw_config_path() == ws.config_path()
    assert doctor._openclaw_config_path() == tmp_path / "openclaw.json"


def test_doctor_empty_env_falls_back_like_config_path(monkeypatch):
    """空串覆盖：doctor 旧实现返回 Path("")（相对路径），config_path() 回落默认目录。"""
    import easel.commands.doctor as doctor

    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", "")
    assert doctor._openclaw_config_path() == ws.config_path()
    assert doctor._openclaw_config_path() == Path.home() / ".openclaw-easel" / "openclaw.json"


def test_doctor_no_direct_path_concat():
    """源码级：doctor 不允许再出现直连 .openclaw-easel 的路径拼接。"""
    src = (PROJECT_ROOT / "easel" / "commands" / "doctor.py").read_text(encoding="utf-8")
    assert 'Path.home() / ".openclaw-easel"' not in src
    assert "Path.home() / '.openclaw-easel'" not in src


@pytest.mark.parametrize("api_key,primary,expected", [
    ("sk-custom-test", "custom/model", True),
    ("", "custom/model", False),
    ("sk-REPLACE_ME", "custom/model", False),
    ("sk-custom-test", "missing/model", False),
    ("sk-custom-test", "custom/", False),
])
def test_doctor_recognizes_primary_provider_key(tmp_path, monkeypatch, api_key, primary, expected):
    import easel.commands.doctor as doctor

    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(doctor, "read_settings", lambda path: {})
    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-REPLACE_ME\n", encoding="utf-8")
    (tmp_path / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {"model": {"primary": primary}}},
        "models": {"providers": {"custom": {"apiKey": api_key}}},
    }), encoding="utf-8")

    assert doctor._env_key_valid() is expected


@pytest.mark.parametrize("content", ["{", "null", '{"agents": null}'])
def test_doctor_invalid_config_does_not_hide_env_credentials(tmp_path, monkeypatch, content):
    import easel.commands.doctor as doctor

    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(doctor, "read_settings", lambda path: {})
    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    (tmp_path / "openclaw.json").write_text(content, encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-env-test\n", encoding="utf-8")

    assert doctor._env_key_valid()
