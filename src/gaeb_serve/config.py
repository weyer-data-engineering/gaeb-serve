"""Settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from gaeb_serve import __version__


@dataclass(frozen=True)
class Settings:
    api_keys: tuple[str, ...]
    max_file_size_bytes: int
    prog_system: str
    prog_name: str

    @classmethod
    def from_env(cls) -> Settings:
        # Several keys, comma-separated, allow rotating a key without downtime.
        api_keys = tuple(key.strip() for key in os.getenv("GAEB_API_KEY", "").split(",") if key.strip())
        if not api_keys:
            raise RuntimeError("GAEB_API_KEY is not set. Set it to the key clients send in the X-API-Key header.")
        return cls(
            api_keys=api_keys,
            max_file_size_bytes=int(os.getenv("GAEB_MAX_FILE_SIZE_MB", "100")) * 1024 * 1024,
            prog_system=os.getenv("GAEB_PROG_SYSTEM", f"gaeb-serve {__version__}"),
            prog_name=os.getenv("GAEB_PROG_NAME", "gaeb-serve"),
        )
