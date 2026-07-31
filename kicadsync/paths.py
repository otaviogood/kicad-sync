"""Machine-independent path resolution for kicadsync.

Nothing in this package hardcodes a user-specific path. Locations resolve in
this order:

1. Environment variable
2. ``~/.config/kicadsync/config.json``
3. Default

Config file keys mirror the env var names without the prefix, lowercased::

    { "cache_dir": "...", "lib_dir": "..." }
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path.home() / ".config" / "kicadsync" / "config.json"


def _config_value(key: str) -> Optional[str]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get(key)
    return str(value) if value else None


def _resolve(env_var: str, config_key: str, default: Optional[Path]) -> Optional[Path]:
    raw = os.environ.get(env_var) or _config_value(config_key)
    if raw:
        return Path(raw).expanduser()
    return default


def cache_dir() -> Path:
    """Regenerable caches: symbol dump, JLC catalog. Never committed anywhere."""
    return _resolve("KICADSYNC_CACHE_DIR", "cache_dir", Path.home() / ".cache" / "kicadsync")


def lib_dir() -> Optional[Path]:
    """The user's personal parts library (footprints.pretty, 3dshapes, datasheets).

    No default: returns None when unconfigured, and features that need it say so.
    """
    return _resolve("KICADSYNC_LIB_DIR", "lib_dir", None)


def libs_json_path() -> Path:
    """Cached dump of installed KiCad symbols, produced by ``kicadsync list-libs``."""
    return cache_dir() / "libs.json"


def jlc_cache_dir() -> Path:
    """Cached JLCPCB/LCSC catalog (from the jlcparts static dataset)."""
    return cache_dir() / "jlc"
