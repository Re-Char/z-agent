import os

import pytest
from pydantic import ValidationError

from zagent.config import AppSettings, ModelSettings
from zagent.security import SecretStore


def test_settings_round_trip(tmp_path):
    settings = AppSettings(data_dir=str(tmp_path), recent_event_limit=42)
    settings.save()
    loaded = AppSettings.load(str(tmp_path))
    assert loaded.recent_event_limit == 42
    assert loaded.model.provider == "echo"


def test_non_echo_provider_requires_base_url():
    with pytest.raises(ValidationError):
        ModelSettings(provider="qwen")


def test_context_ratios_must_be_ordered():
    with pytest.raises(ValidationError):
        ModelSettings(soft_limit_ratio=0.9, hard_limit_ratio=0.8)


def test_secret_store_round_trip_and_environment_precedence(tmp_path, monkeypatch):
    secrets = SecretStore(str(tmp_path))
    secrets.set("MODEL_KEY", "file-value")
    assert secrets.get("MODEL_KEY") == "file-value"
    monkeypatch.setenv("MODEL_KEY", "env-value")
    assert secrets.get("MODEL_KEY") == "env-value"
    assert os.stat(tmp_path / "secrets.json").st_mode & 0o777 == 0o600

