import json
import os

import pytest
from pydantic import ValidationError

from zagent.config import (
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_OFFICIAL_BASE_URL,
    AppSettings,
    ModelSettings,
)
from zagent.security import SecretStore


def test_settings_round_trip(tmp_path):
    settings = AppSettings(data_dir=str(tmp_path), recent_event_limit=42)
    settings.save()
    loaded = AppSettings.load(str(tmp_path))
    assert loaded.recent_event_limit == 42
    assert loaded.model.provider == "echo"


def test_legacy_recent_event_default_migrates_without_overwriting_custom_value(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"recent_event_limit": 24}), encoding="utf-8")
    assert AppSettings.load(str(tmp_path)).recent_event_limit == 96

    config_path.write_text(json.dumps({"recent_event_limit": 42}), encoding="utf-8")
    assert AppSettings.load(str(tmp_path)).recent_event_limit == 42


def test_non_echo_provider_requires_base_url():
    with pytest.raises(ValidationError):
        ModelSettings(provider="qwen")


def test_context_ratios_must_be_ordered():
    with pytest.raises(ValidationError):
        ModelSettings(soft_limit_ratio=0.9, hard_limit_ratio=0.8)


def test_deepseek_invalid_model_name_is_migrated():
    # The early UI suggested the provider name ("deepseek") as the model name,
    # which DeepSeek rejects with 400.  Loading such a config must heal it.
    settings = ModelSettings(
        provider="DeepSeek",  # case/whitespace is normalized too
        model="deepseek",
        base_url="https://api.deepseek.com/v1/",
    )
    assert settings.provider == "deepseek"
    assert settings.model == DEEPSEEK_DEFAULT_MODEL
    assert settings.model != "deepseek"
    assert settings.base_url == DEEPSEEK_OFFICIAL_BASE_URL


def test_deepseek_official_endpoint_is_normalized():
    settings = ModelSettings(
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1",
    )
    assert settings.base_url == DEEPSEEK_OFFICIAL_BASE_URL
    assert settings.model == "deepseek-v4-flash"


def test_model_name_is_required():
    with pytest.raises(ValidationError):
        ModelSettings(provider="openai_compatible", model=" ", base_url="https://x/v1")


def test_single_model_config_migrates_to_profiles(tmp_path):
    # v1 config.json stored a lone `model` object; loading must promote it.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "model": {"provider": "deepseek", "model": "deepseek",
                  "base_url": "https://api.deepseek.com/v1", "context_window": 1000000},
    }), encoding="utf-8")
    settings = AppSettings.load(str(tmp_path))
    assert len(settings.models) == 1
    assert settings.active_model_id == settings.models[0].id
    assert settings.active_model.provider == "deepseek"
    assert settings.active_model.model == DEEPSEEK_DEFAULT_MODEL
    assert settings.model.id == settings.active_model_id


def test_upsert_adds_and_activates_new_profile():
    settings = AppSettings(data_dir="/tmp/x")
    default_id = settings.models[0].id  # fresh configs start with the echo demo profile
    first = settings.upsert_model({"name": "演示", "provider": "echo"})
    assert settings.active_model_id == first.id
    second = settings.upsert_model({"name": "DeepSeek", "provider": "deepseek",
                                    "model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com"})
    assert len(settings.models) == 3
    assert settings.active_model_id == second.id
    assert settings.active_model.name == "DeepSeek"
    assert default_id != second.id


def test_upsert_updates_existing_profile_by_id():
    settings = AppSettings(data_dir="/tmp/x")
    profile = settings.upsert_model({"name": "A", "provider": "echo"})
    updated = settings.upsert_model({"id": profile.id, "name": "A 改", "provider": "openai_compatible",
                                     "model": "qwen", "base_url": "https://x/v1"})
    assert updated.name == "A 改"
    assert settings.active_model.id == profile.id
    assert settings.active_model.provider == "openai_compatible"
    assert len(settings.models) == 2  # default echo demo + A


def test_delete_and_activate_model():
    settings = AppSettings(data_dir="/tmp/x")
    default_id = settings.models[0].id
    a = settings.upsert_model({"name": "A", "provider": "echo"})
    settings.upsert_model({"name": "B", "provider": "echo"})
    c = settings.upsert_model({"name": "C", "provider": "echo"})
    assert settings.active_model_id == c.id
    assert settings.activate_model(a.id)
    assert settings.active_model_id == a.id
    assert not settings.activate_model("missing")
    assert settings.delete_model(a.id)
    # deleting the active model falls back to the first remaining profile
    assert settings.active_model_id == default_id
    assert settings.model.id == default_id
    assert len(settings.models) == 3
    assert not settings.delete_model("missing")


def test_round_trip_preserves_profiles_and_active(tmp_path):
    settings = AppSettings(data_dir=str(tmp_path))
    settings.upsert_model({"name": "DeepSeek", "provider": "deepseek",
                           "model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com"})
    settings.save()
    loaded = AppSettings.load(str(tmp_path))
    assert len(loaded.models) == 2
    assert loaded.active_model.name == "DeepSeek"
    assert loaded.model.id == loaded.active_model_id


def test_secret_store_round_trip_and_environment_precedence(tmp_path, monkeypatch):
    secrets = SecretStore(str(tmp_path))
    secrets.set("MODEL_KEY", "file-value")
    assert secrets.get("MODEL_KEY") == "file-value"
    monkeypatch.setenv("MODEL_KEY", "env-value")
    assert secrets.get("MODEL_KEY") == "env-value"
    assert os.stat(tmp_path / "secrets.json").st_mode & 0o777 == 0o600
