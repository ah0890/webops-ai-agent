"""Load and validate the user's YAML/JSON configuration files."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.config.schema import WebsitesFile, WorkflowsFile


class ConfigError(Exception):
    pass


def _read(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    return data or {}


def _fmt(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())


def load_websites(path: Path) -> WebsitesFile:
    if not path.exists():
        return WebsitesFile()
    try:
        return WebsitesFile.model_validate(_read(path))
    except ValidationError as e:
        raise ConfigError(f"{path}: {_fmt(e)}") from e


def load_workflows(path: Path) -> WorkflowsFile:
    if not path.exists():
        return WorkflowsFile()
    try:
        return WorkflowsFile.model_validate(_read(path))
    except ValidationError as e:
        raise ConfigError(f"{path}: {_fmt(e)}") from e
