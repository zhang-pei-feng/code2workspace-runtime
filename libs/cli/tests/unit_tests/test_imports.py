"""Test importing files."""

import pytest


def test_imports() -> None:
    """Test importing code2workspace modules."""
    from code2workspace_cli import (
        agent,
        integrations,
    )
    from code2workspace_cli.main import cli_main


class TestLazyPackageGetattr:
    """Tests for __init__.py lazy __getattr__ resolution."""

    def test_cli_main_via_package(self) -> None:
        """Package-level __getattr__ resolves cli_main lazily."""
        from code2workspace_cli import cli_main

        assert callable(cli_main)

    def test_unknown_attr_raises(self) -> None:
        """Accessing an unknown attribute raises AttributeError."""
        import code2workspace_cli

        with pytest.raises(AttributeError, match="has no attribute"):
            getattr(code2workspace_cli, "nonexistent_xyz")  # noqa: B009
