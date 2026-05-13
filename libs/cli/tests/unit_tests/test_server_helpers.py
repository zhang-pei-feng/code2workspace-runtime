"""Tests for extracted helper functions in server.py."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from code2workspace_cli.server import (
    _build_server_cmd,
    _build_server_env,
    _scoped_env_overrides,
)


class TestBuildServerCmd:
    def test_contains_host_and_port(self) -> None:
        cmd = _build_server_cmd(Path("/tmp/lg.json"), host="0.0.0.0", port=3000)
        assert "--host" in cmd
        assert "0.0.0.0" in cmd
        assert "--port" in cmd
        assert "3000" in cmd

    def test_contains_config_path(self) -> None:
        p = Path("/work/langgraph.json")
        cmd = _build_server_cmd(p, host="127.0.0.1", port=2024)
        assert str(p) in cmd

    def test_includes_no_browser_and_no_reload(self) -> None:
        cmd = _build_server_cmd(Path("/tmp/lg.json"), host="127.0.0.1", port=2024)
        assert "--no-browser" in cmd
        assert "--no-reload" in cmd

    def test_includes_allow_blocking(self) -> None:
        cmd = _build_server_cmd(Path("/tmp/lg.json"), host="127.0.0.1", port=2024)
        assert "--allow-blocking" in cmd


class TestBuildServerEnv:
    def test_sets_auth_noop(self) -> None:
        env = _build_server_env()
        assert env["LANGGRAPH_AUTH_TYPE"] == "noop"

    def test_strips_auth_variables(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LANGGRAPH_AUTH": "secret",
                "LANGGRAPH_CLOUD_LICENSE_KEY": "key",
                "LANGSMITH_CONTROL_PLANE_API_KEY": "cpkey",
                "LANGSMITH_TENANT_ID": "tid",
            },
        ):
            env = _build_server_env()
        assert "LANGGRAPH_AUTH" not in env
        assert "LANGGRAPH_CLOUD_LICENSE_KEY" not in env
        assert "LANGSMITH_CONTROL_PLANE_API_KEY" not in env
        assert "LANGSMITH_TENANT_ID" not in env

    def test_sets_pythondontwritebytecode(self) -> None:
        env = _build_server_env()
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_strips_empty_langsmith_tracing_flags(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LANGSMITH_TRACING": "",
                "LANGCHAIN_TRACING_V2": "",
                "LANGSMITH_API_KEY": "",
            },
        ):
            env = _build_server_env()

        assert "LANGSMITH_TRACING" not in env
        assert "LANGCHAIN_TRACING_V2" not in env
        assert env["LANGSMITH_API_KEY"] == ""

    def test_preserves_nonempty_langsmith_tracing_flags(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "true",
            },
        ):
            env = _build_server_env()

        assert env["LANGSMITH_TRACING"] == "false"
        assert env["LANGCHAIN_TRACING_V2"] == "true"


class TestScopedEnvOverrides:
    def test_overrides_applied_inside_context(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            _scoped_env_overrides({"TEST_SCOPED_VAR": "val"}),
        ):
            assert os.environ.get("TEST_SCOPED_VAR") == "val"

    def test_overrides_kept_on_success(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            with _scoped_env_overrides({"TEST_SCOPED_KEEP": "val"}):
                pass
            assert os.environ.get("TEST_SCOPED_KEEP") == "val"

    def test_overrides_rolled_back_on_exception(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            msg = "boom"
            with (
                pytest.raises(RuntimeError),
                _scoped_env_overrides({"TEST_SCOPED_ROLL": "new"}),
            ):
                raise RuntimeError(msg)
            assert os.environ.get("TEST_SCOPED_ROLL") is None

    def test_previous_value_restored_on_exception(self) -> None:
        msg = "boom"
        with patch.dict(os.environ, {"TEST_SCOPED_PREV": "original"}, clear=False):
            with (
                pytest.raises(RuntimeError),
                _scoped_env_overrides({"TEST_SCOPED_PREV": "new"}),
            ):
                raise RuntimeError(msg)
            assert os.environ["TEST_SCOPED_PREV"] == "original"
