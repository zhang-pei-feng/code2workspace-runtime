"""Shared model-settings helpers for the web workbench."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Any

from dotenv import dotenv_values
import tomli_w

from code2workspace_cli.config import create_model
from code2workspace_cli.model_config import DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_PATH, ModelConfig, clear_caches


THEME_MODES = ("light", "dark", "system")

_OPENAI_COMPATIBLE_CLASS_PATH = "langchain_openai:ChatOpenAI"

_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "key": "openai",
        "provider_key": "openai",
        "label": "OpenAI",
        "provider_kind": "native",
        "base_url": None,
        "api_key_env": "CODE2WORKSPACE_CLI_OPENAI_API_KEY",
    },
    {
        "key": "gemini",
        "provider_key": "google_genai",
        "label": "Gemini",
        "provider_kind": "native",
        "base_url": None,
        "api_key_env": "CODE2WORKSPACE_CLI_GOOGLE_API_KEY",
    },
    {
        "key": "deepseek",
        "provider_key": "deepseek",
        "label": "DeepSeek",
        "provider_kind": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "CODE2WORKSPACE_CLI_DEEPSEEK_API_KEY",
    },
    {
        "key": "kimi",
        "provider_key": "kimi",
        "label": "Kimi",
        "provider_kind": "openai_compatible",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "CODE2WORKSPACE_CLI_KIMI_API_KEY",
    },
    {
        "key": "glm",
        "provider_key": "glm",
        "label": "GLM",
        "provider_kind": "openai_compatible",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "api_key_env": "CODE2WORKSPACE_CLI_GLM_API_KEY",
    },
    {
        "key": "qwen",
        "provider_key": "qwen",
        "label": "Qwen",
        "provider_kind": "openai_compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "CODE2WORKSPACE_CLI_QWEN_API_KEY",
    },
    {
        "key": "custom_openai_compatible",
        "provider_key": None,
        "label": "自定义 OpenAI 兼容",
        "provider_kind": "openai_compatible",
        "base_url": None,
        "api_key_env": None,
    },
)

_TEMPLATES_BY_PROVIDER_KEY = {
    item["provider_key"]: item for item in _TEMPLATES if item["provider_key"] is not None
}


class SettingsValidationError(ValueError):
    """Raised when web settings payload validation fails."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ModelSettingsStore:
    """Read/write shared model settings for the web workbench."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or DEFAULT_CONFIG_DIR).expanduser().resolve()
        self.config_path = self.root / "config.toml"
        self.dotenv_path = self.root / ".env"

    def load_settings_payload(self) -> dict[str, Any]:
        """Return normalized model settings for the browser UI."""
        config = ModelConfig.load(self.config_path)
        env_data = self._read_dotenv()
        providers = [
            self._provider_record_from_config(provider_key, provider_config, env_data)
            for provider_key, provider_config in config.providers.items()
        ]
        return {
            "default_model": config.default_model,
            "providers": providers,
            "templates": [dict(item) for item in _TEMPLATES],
        }

    def load_model_selector_payload(self) -> dict[str, Any]:
        """Return the existing `/api/models` response shape for the chat UI."""
        config = ModelConfig.load(self.config_path)
        models = [
            {
                "spec": f"{provider}:{model}",
                "provider": provider,
                "model": model,
                "label": model,
            }
            for model, provider in config.get_all_models()
            if config.is_provider_enabled(provider)
        ]
        return {
            "models": models,
            "default_model": config.default_model,
            "recent_model": config.recent_model,
        }

    def save_settings_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist the full model-settings payload and return normalized state."""
        providers = payload.get("providers")
        if not isinstance(providers, list):
            raise SettingsValidationError("providers_required", "providers is required")

        raw_config = self._read_raw_config()
        raw_models = raw_config.get("models", {}) if isinstance(raw_config.get("models"), dict) else {}
        existing_provider_configs = (
            raw_models.get("providers", {})
            if isinstance(raw_models.get("providers"), dict)
            else {}
        )
        existing_payload = self.load_settings_payload()
        existing_by_key = {
            provider["key"]: provider for provider in existing_payload["providers"]
        }
        normalized = [self._normalize_provider(provider, existing_by_key) for provider in providers]

        valid_specs = {
            f'{provider["key"]}:{model}'
            for provider in normalized
            if provider["enabled"]
            for model in provider["models"]
        }
        default_model = payload.get("default_model")
        if default_model is not None:
            default_model = str(default_model).strip() or None
        if default_model and default_model not in valid_specs:
            raise SettingsValidationError(
                "invalid_default_model",
                "default_model must reference an enabled configured model",
            )

        self.root.mkdir(parents=True, exist_ok=True)
        models_table: dict[str, Any] = {
            **(
                {"default": default_model}
                if default_model is not None
                else {}
            ),
            **(
                {"recent": raw_models["recent"]}
                if "recent" in raw_models
                else {}
            ),
            "providers": {
                provider["key"]: self._provider_config_for_write(
                    provider,
                    existing_provider_configs.get(provider["key"], {}),
                )
                for provider in normalized
            },
        }
        self._write_toml({"models": models_table})
        self._write_dotenv(normalized, existing_by_key)
        clear_caches()
        return self.load_settings_payload()

    async def test_provider(self, provider_payload: dict[str, Any], model: str) -> dict[str, Any]:
        """Run a lightweight connectivity test for one provider draft."""
        normalized = self._normalize_provider(provider_payload, {})
        temp_root = self.root / ".tmp-settings-test"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_store = ModelSettingsStore(temp_root)
        temp_store._write_toml(
            {
                "models": {
                    "default": f'{normalized["key"]}:{model}',
                    "providers": {
                        normalized["key"]: self._provider_config_for_write(
                            normalized,
                            {},
                        )
                    },
                }
            }
        )
        temp_store._write_dotenv([normalized], {})
        restore_env = self._inject_provider_env(normalized)
        previous_default_path = DEFAULT_CONFIG_PATH
        try:
            from code2workspace_cli import model_config as model_config_module

            model_config_module.DEFAULT_CONFIG_PATH = temp_store.config_path
            clear_caches()
            model_result = create_model(f'{normalized["key"]}:{model}')
            await model_result.model.ainvoke("Reply with OK only.")
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "provider_key": normalized["key"],
                "model": model,
                "message": str(exc),
            }
        finally:
            from code2workspace_cli import model_config as model_config_module

            model_config_module.DEFAULT_CONFIG_PATH = previous_default_path
            restore_env()
            clear_caches()
            with contextlib.suppress(OSError):
                if temp_store.config_path.exists():
                    temp_store.config_path.unlink()
            with contextlib.suppress(OSError):
                if temp_store.dotenv_path.exists():
                    temp_store.dotenv_path.unlink()
            with contextlib.suppress(OSError):
                temp_root.rmdir()

        return {
            "ok": True,
            "provider_key": normalized["key"],
            "model": model,
            "message": "Connection test succeeded.",
        }

    def load_appearance_payload(self) -> dict[str, Any]:
        """Return supported browser-side appearance settings."""
        return {
            "theme_modes": list(THEME_MODES),
            "storage": "browser",
            "default_theme": "system",
        }

    def save_appearance_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate one appearance payload and echo the browser-owned choice."""
        theme = str(payload.get("theme") or "").strip().lower()
        if theme not in THEME_MODES:
            raise SettingsValidationError("invalid_theme", "theme must be light, dark, or system")
        return {"theme": theme, "storage": "browser"}

    def _normalize_provider(
        self,
        payload: dict[str, Any],
        existing_by_key: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        key = str(payload.get("key") or "").strip()
        if not key:
            raise SettingsValidationError("provider_key_required", "provider key is required")
        label = str(payload.get("label") or "").strip()
        if not label:
            raise SettingsValidationError("provider_label_required", "provider label is required")
        provider_kind = str(payload.get("provider_kind") or "").strip()
        if provider_kind not in {"native", "openai_compatible"}:
            raise SettingsValidationError(
                "invalid_provider_kind",
                "provider_kind must be native or openai_compatible",
            )
        enabled = bool(payload.get("enabled", True))
        models = [
            str(item).strip()
            for item in payload.get("models", [])
            if str(item).strip()
        ]
        if not models:
            raise SettingsValidationError("provider_models_required", "provider models cannot be empty")
        base_url = payload.get("base_url")
        base_url = str(base_url).strip() if base_url is not None else None
        if provider_kind == "openai_compatible" and not base_url:
            raise SettingsValidationError(
                "base_url_required",
                "openai compatible providers require a base_url",
            )
        api_key_env = payload.get("api_key_env")
        api_key_env = str(api_key_env).strip() if api_key_env is not None else None
        existing = existing_by_key.get(key)
        if not api_key_env:
            api_key_env = existing.get("api_key_env") if existing else None
        api_key = payload.get("api_key")
        api_key = str(api_key).strip() if api_key is not None else None
        has_existing_key = bool(existing and existing.get("has_api_key"))
        if not api_key_env:
            raise SettingsValidationError("api_key_env_required", "api_key_env is required")
        if not api_key and not has_existing_key:
            raise SettingsValidationError("api_key_required", "api_key is required")
        template = _TEMPLATES_BY_PROVIDER_KEY.get(key)
        template_key = str(payload.get("template_key") or "").strip() or (
            template["key"] if template else None
        )
        test_model = str(payload.get("test_model") or models[0]).strip()
        return {
            "key": key,
            "label": label,
            "enabled": enabled,
            "provider_kind": provider_kind,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "api_key": api_key,
            "models": models,
            "test_model": test_model,
            "template_key": template_key,
            "has_api_key": bool(api_key or has_existing_key),
        }

    def _provider_config_for_write(
        self,
        provider: dict[str, Any],
        existing_config: dict[str, Any],
    ) -> dict[str, Any]:
        config: dict[str, Any] = dict(existing_config)
        config.update(
            {
                "models": provider["models"],
                "api_key_env": provider["api_key_env"],
                "enabled": provider["enabled"],
                "display_name": provider["label"],
                "provider_kind": provider["provider_kind"],
            }
        )
        if provider.get("template_key"):
            config["template_key"] = provider["template_key"]
        if provider["provider_kind"] == "openai_compatible":
            config["base_url"] = provider["base_url"]
            config["class_path"] = _OPENAI_COMPATIBLE_CLASS_PATH
        elif provider.get("base_url"):
            config["base_url"] = provider["base_url"]
        return config

    def _provider_record_from_config(
        self,
        provider_key: str,
        provider_config: dict[str, Any],
        env_data: dict[str, str | None],
    ) -> dict[str, Any]:
        template = _TEMPLATES_BY_PROVIDER_KEY.get(provider_key)
        api_key_env = provider_config.get("api_key_env")
        has_api_key = bool(api_key_env and env_data.get(api_key_env))
        provider_kind = provider_config.get("provider_kind")
        if not provider_kind:
            provider_kind = (
                "openai_compatible"
                if provider_config.get("class_path") == _OPENAI_COMPATIBLE_CLASS_PATH
                else "native"
            )
        models = list(provider_config.get("models", []))
        return {
            "key": provider_key,
            "label": provider_config.get("display_name")
            or (template["label"] if template else provider_key),
            "enabled": provider_config.get("enabled", True) is not False,
            "provider_kind": provider_kind,
            "base_url": provider_config.get("base_url"),
            "api_key_env": api_key_env,
            "has_api_key": has_api_key,
            "models": models,
            "test_model": models[0] if models else None,
            "template_key": provider_config.get("template_key")
            or (template["key"] if template else None),
        }

    def _write_toml(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                tomli_w.dump(data, handle)
            Path(tmp_path).replace(self.config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise

    def _read_dotenv(self) -> dict[str, str | None]:
        if not self.dotenv_path.exists():
            return {}
        values = dotenv_values(self.dotenv_path)
        return {
            key: value
            for key, value in values.items()
            if key is not None
        }

    def _write_dotenv(
        self,
        providers: list[dict[str, Any]],
        existing_by_key: dict[str, dict[str, Any]],
    ) -> None:
        env_data = self._read_dotenv()
        for provider in providers:
            env_name = provider["api_key_env"]
            api_key = provider.get("api_key")
            previous_env_name = None
            existing = existing_by_key.get(provider["key"])
            if existing:
                previous_env_name = existing.get("api_key_env")
            if api_key:
                env_data[env_name] = api_key
            elif previous_env_name and previous_env_name != env_name and env_data.get(previous_env_name):
                env_data[env_name] = env_data.get(previous_env_name)
            elif previous_env_name:
                env_data[env_name] = env_data.get(env_name)
            if previous_env_name and previous_env_name != env_name:
                env_data.pop(previous_env_name, None)
        lines = [
            f"{key}={self._format_env_value(value)}"
            for key, value in sorted(env_data.items())
            if value is not None
        ]
        fd, tmp_path = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
                if lines:
                    handle.write("\n")
            Path(tmp_path).replace(self.dotenv_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise

    def _format_env_value(self, value: str | None) -> str:
        if value is None:
            return ""
        if any(char.isspace() for char in value) or any(char in value for char in {'"', "#"}):
            return json.dumps(value)
        return value

    def _read_raw_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        with self.config_path.open("rb") as handle:
            return tomllib.load(handle)

    def _inject_provider_env(self, provider: dict[str, Any]) -> Any:
        env_name = provider["api_key_env"]
        prior = os.environ.get(env_name)
        prior_present = env_name in os.environ
        if provider.get("api_key"):
            os.environ[env_name] = provider["api_key"]

        def restore() -> None:
            if prior_present:
                os.environ[env_name] = prior or ""
                return
            os.environ.pop(env_name, None)

        return restore
