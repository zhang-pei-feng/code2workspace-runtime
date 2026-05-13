from __future__ import annotations

import tomllib
from pathlib import Path

from apps.webapp.settings_models import ModelSettingsStore


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_save_settings_preserves_recent_model_and_unknown_provider_fields(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".code2workspace"
    write_file(
        root / "config.toml",
        """
[models]
default = "openai:gpt-4.1"
recent = "openai:gpt-4.1"

[models.providers.openai]
models = ["gpt-4.1"]
api_key_env = "CODE2WORKSPACE_CLI_OPENAI_API_KEY"
enabled = true
params = { temperature = 0.2 }
profile = { max_input_tokens = 12345 }
""".strip()
        + "\n",
    )
    write_file(root / ".env", "CODE2WORKSPACE_CLI_OPENAI_API_KEY=sk-old\n")
    store = ModelSettingsStore(root)

    store.save_settings_payload(
        {
            "default_model": "openai:gpt-5.4",
            "providers": [
                {
                    "key": "openai",
                    "label": "OpenAI",
                    "enabled": True,
                    "provider_kind": "native",
                    "base_url": None,
                    "api_key_env": "CODE2WORKSPACE_CLI_OPENAI_API_KEY",
                    "api_key": "",
                    "models": ["gpt-5.4"],
                    "test_model": "gpt-5.4",
                    "template_key": "openai",
                }
            ],
        }
    )

    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    openai = config["models"]["providers"]["openai"]
    assert config["models"]["recent"] == "openai:gpt-4.1"
    assert openai["params"] == {"temperature": 0.2}
    assert openai["profile"] == {"max_input_tokens": 12345}
    assert openai["models"] == ["gpt-5.4"]


def test_save_settings_moves_existing_api_key_when_env_name_changes(tmp_path: Path) -> None:
    root = tmp_path / ".code2workspace"
    write_file(
        root / "config.toml",
        """
[models]
default = "relay_demo:moonshot-v1-8k"

[models.providers.relay_demo]
models = ["moonshot-v1-8k"]
api_key_env = "CODE2WORKSPACE_CLI_OLD_RELAY_KEY"
enabled = true
base_url = "https://relay.example/v1"
class_path = "langchain_openai:ChatOpenAI"
provider_kind = "openai_compatible"
""".strip()
        + "\n",
    )
    write_file(root / ".env", "CODE2WORKSPACE_CLI_OLD_RELAY_KEY=relay-secret\n")
    store = ModelSettingsStore(root)

    store.save_settings_payload(
        {
            "default_model": "relay_demo:moonshot-v1-8k",
            "providers": [
                {
                    "key": "relay_demo",
                    "label": "Demo Relay",
                    "enabled": True,
                    "provider_kind": "openai_compatible",
                    "base_url": "https://relay.example/v1",
                    "api_key_env": "CODE2WORKSPACE_CLI_NEW_RELAY_KEY",
                    "api_key": "",
                    "models": ["moonshot-v1-8k"],
                    "test_model": "moonshot-v1-8k",
                    "template_key": "custom_openai_compatible",
                }
            ],
        }
    )

    dotenv_text = (root / ".env").read_text(encoding="utf-8")
    assert "CODE2WORKSPACE_CLI_NEW_RELAY_KEY=relay-secret" in dotenv_text
    assert "CODE2WORKSPACE_CLI_OLD_RELAY_KEY" not in dotenv_text


def test_save_settings_removes_non_default_provider_without_touching_default(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".code2workspace"
    write_file(
        root / "config.toml",
        """
[models]
default = "openai:gpt-5.4"
recent = "openai:gpt-5.4"

[models.providers.openai]
models = ["gpt-5.4"]
api_key_env = "OPENAI_API_KEY"
enabled = true

[models.providers.anthropic]
models = ["claude-sonnet-4-6"]
api_key_env = "ANTHROPIC_API_KEY"
enabled = true
""".strip()
        + "\n",
    )
    write_file(root / ".env", "OPENAI_API_KEY=aaa\nANTHROPIC_API_KEY=bbb\n")
    store = ModelSettingsStore(root)
    current = store.load_settings_payload()

    result = store.save_settings_payload(
        {
            "default_model": "openai:gpt-5.4",
            "providers": [provider for provider in current["providers"] if provider["key"] != "anthropic"],
        }
    )

    assert [provider["key"] for provider in result["providers"]] == ["openai"]
    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    assert config["models"]["default"] == "openai:gpt-5.4"
    assert "anthropic" not in config["models"]["providers"]


def test_save_settings_clears_default_key_when_removed_provider_owned_default(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".code2workspace"
    write_file(
        root / "config.toml",
        """
[models]
default = "anthropic:claude-sonnet-4-6"
recent = "openai:gpt-5.4"

[models.providers.openai]
models = ["gpt-5.4"]
api_key_env = "OPENAI_API_KEY"
enabled = true

[models.providers.anthropic]
models = ["claude-sonnet-4-6"]
api_key_env = "ANTHROPIC_API_KEY"
enabled = true
""".strip()
        + "\n",
    )
    write_file(root / ".env", "OPENAI_API_KEY=aaa\nANTHROPIC_API_KEY=bbb\n")
    store = ModelSettingsStore(root)
    current = store.load_settings_payload()

    result = store.save_settings_payload(
        {
            "default_model": None,
            "providers": [provider for provider in current["providers"] if provider["key"] != "anthropic"],
        }
    )

    assert result["default_model"] is None
    assert [provider["key"] for provider in result["providers"]] == ["openai"]
    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    assert "default" not in config["models"]
    assert config["models"]["recent"] == "openai:gpt-5.4"
    assert "anthropic" not in config["models"]["providers"]
