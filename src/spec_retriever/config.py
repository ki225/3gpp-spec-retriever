"""User-level configuration for managed official corpora."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "3gpp-spec-retriever" / "config.yaml"


@dataclass(frozen=True)
class Config:
    release: int
    storage_root: Path
    retrieval: str = "sqlite"
    scope: str = "all"

    @property
    def database(self) -> Path:
        return self.storage_root / "corpus.sqlite3"

    @property
    def active_pointer(self) -> Path:
        return self.storage_root / "current.json"


def load_config(path: str | Path | None = None) -> Config:
    try:
        import yaml
    except ImportError as error:
        raise ValueError("install PyYAML to use a YAML configuration") from error
    path = Path(path) if path is not None else default_config_path()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"configuration file not found: {path}") from error
    except yaml.YAMLError as error:
        raise ValueError(f"invalid YAML configuration: {path}") from error
    if not isinstance(raw, dict) or set(raw) - {"release", "scope", "storage_root", "retrieval"}:
        raise ValueError("configuration must contain only release, scope, storage_root and retrieval")
    release = raw.get("release")
    if type(release) is not int or not 1 <= release <= 35:
        raise ValueError("release must be an integer between 1 and 35")
    if raw.get("scope", "all") != "all":
        raise ValueError("this build supports scope: all; series profiles are not implemented")
    storage = raw.get("storage_root")
    if not isinstance(storage, str) or not Path(storage).expanduser().is_absolute():
        raise ValueError("storage_root must be an absolute path")
    retrieval = raw.get("retrieval", "sqlite")
    if retrieval not in {"sqlite", "elastic"}:
        raise ValueError("retrieval must be sqlite or elastic")
    return Config(release, Path(storage).expanduser().resolve(), retrieval)
