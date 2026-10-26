import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    root: Path = field(
        default_factory=lambda: Path(os.getenv("FONT_MANAGER_ROOT", "fonts"))
    )
    state: Path = field(default_factory=lambda: Path(os.getenv("FONT_MANAGER_STATE", ".state")).resolve())
    # Optional one-time seed, including migration from older deployments.
    token: str = field(
        default_factory=lambda: os.getenv("FONT_MANAGER_API_TOKEN", os.getenv("FONT_MANAGER_TOKEN", ""))
    )
    host: str = field(default_factory=lambda: os.getenv("FONT_MANAGER_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("FONT_MANAGER_PORT", "8765")))

    def __post_init__(self):
        self.root = self.root.expanduser().absolute()
        self.state = self.state.expanduser().absolute()
        self.state.mkdir(parents=True, exist_ok=True)
