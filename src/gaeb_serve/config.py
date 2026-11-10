"""Settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from gaeb_serve import __version__


@dataclass(frozen=True)
class Settings:
    max_file_size_bytes: int
    prog_system: str
    prog_name: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            max_file_size_bytes=int(os.getenv("GAEB_MAX_FILE_SIZE_MB", "100")) * 1024 * 1024,
            prog_system=os.getenv("GAEB_PROG_SYSTEM", f"gaeb-serve {__version__}"),
            prog_name=os.getenv("GAEB_PROG_NAME", "gaeb-serve"),
        )
