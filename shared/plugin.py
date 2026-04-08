"""Shared plugin interface and context."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine


@dataclass
class PluginContext:
    client: Any
    bridge: Any
    state: Any
    log: Any
    project_root: Path
    documents_dir: Path
    owner_id: int
    env: dict[str, str]
    register_task: Callable[[Coroutine], None]
    extra: dict[str, Any] = field(default_factory=dict)


class Plugin:
    name: str = ""
    actions: list[str] = []
    events: list[str] = []
    commands: list[str] = []

    async def setup(self, ctx: PluginContext) -> None:
        return None

    async def teardown(self) -> None:
        return None

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        return {}

    async def on_event(self, event: str, *args, **kwargs) -> None:
        return None

    async def handle_command(self, cmd: str, message, channel) -> bool:
        return False

    def build_prompt_section(self, **kwargs) -> str | None:
        return None

    def strip_text(self, text: str, **kwargs) -> str:
        return text

    async def process_text(self, text: str, **kwargs) -> str:
        return text
