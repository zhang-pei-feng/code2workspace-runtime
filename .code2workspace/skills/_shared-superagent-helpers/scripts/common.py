#!/usr/bin/env python3
"""Shared helper functions for project-level superagent skills."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    """Return the repository root based on this file location."""
    return Path(__file__).resolve().parents[4]


def skills_root() -> Path:
    """Return the project skills root."""
    return repo_root() / ".code2workspace" / "skills"


def results_root() -> Path:
    """Return the shared results root."""
    return repo_root() / "results" / "skills"


def utc_timestamp() -> str:
    """Return a filesystem-friendly UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slugify(value: str, default: str = "run") -> str:
    """Return a conservative slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-_.").lower()
    return cleaned or default


def ensure_dir(path: Path) -> Path:
    """Create a directory if needed and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_skill_run_dir(skill_name: str, *parts: str) -> Path:
    """Create a timestamped run directory under `results/skills/<skill>/`."""
    base = ensure_dir(results_root() / skill_name)
    for part in parts:
        base = ensure_dir(base / slugify(part))
    run_dir = ensure_dir(base / utc_timestamp())
    return run_dir


def write_json(path: Path, payload: Any) -> Path:
    """Write JSON with UTF-8 encoding."""
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    """Read JSON content."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, content: str) -> Path:
    """Write text with UTF-8 encoding."""
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")
    return path


def latest_child_dir(path: Path) -> Path | None:
    """Return the lexicographically latest child directory."""
    if not path.exists():
        return None
    dirs = [item for item in path.iterdir() if item.is_dir()]
    if not dirs:
        return None
    return sorted(dirs)[-1]
