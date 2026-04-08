"""Plugin discovery and dispatch manager."""

import importlib
from typing import Any

from shared.plugin import Plugin
from shared.plugin import PluginContext


def _normalize_action_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    results = payload.get("results")
    if results is None:
        result = payload.get("result")
        if isinstance(result, str) and result:
            results = [result]
        else:
            results = []
    elif isinstance(results, str):
        results = [results]
    elif not isinstance(results, list):
        results = [str(results)]
    files = payload.get("files") or []
    reload_flag = bool(payload.get("reload", False))
    council_feedback = payload.get("council_feedback") or []
    return {
        "results": results,
        "files": files,
        "reload": reload_flag,
        "council_feedback": council_feedback,
    }


class PluginManager:
    def __init__(self, plugins: list[Plugin], ctx: PluginContext):
        self.plugins = plugins
        self.ctx = ctx
        self._action_map: dict[str, Plugin] = {}
        for plugin in plugins:
            for action in getattr(plugin, "actions", []) or []:
                if action in self._action_map:
                    ctx.log.warning(
                        f"Plugin action conflict: '{action}' claimed by "
                        f"{self._action_map[action].name} and {plugin.name}; first wins"
                    )
                    continue
                self._action_map[action] = plugin

    async def dispatch_action(
        self, action_name: str, action_dict: dict, message, channel, guild_id: int, **kwargs
    ) -> tuple[bool, dict[str, Any]]:
        plugin = self._action_map.get(action_name)
        if not plugin:
            return False, _normalize_action_result(None)
        try:
            payload = await plugin.handle_action(action_dict, message, channel, guild_id, **kwargs)
            return True, _normalize_action_result(payload)
        except Exception:
            self.ctx.log.exception(f"Plugin action failed: {plugin.name}:{action_name}")
            return True, _normalize_action_result(
                {"results": [f"{plugin.name}:{action_name} failed (see logs)"], "reload": False}
            )

    def get_prompt_sections(self, **kwargs) -> list[str]:
        sections: list[str] = []
        for plugin in self.plugins:
            try:
                section = plugin.build_prompt_section(**kwargs)
                if section:
                    sections.append(section)
            except Exception:
                self.ctx.log.exception(f"Plugin prompt section failed: {plugin.name}")
        return sections

    def strip_text_for_display(self, text: str, **kwargs) -> str:
        current = text
        for plugin in self.plugins:
            try:
                updated = plugin.strip_text(current, **kwargs)
                if isinstance(updated, str):
                    current = updated
            except Exception:
                self.ctx.log.exception(f"Plugin strip_text failed: {plugin.name}")
        return current

    async def process_text(self, text: str, **kwargs) -> str:
        current = text
        for plugin in self.plugins:
            try:
                updated = await plugin.process_text(current, **kwargs)
                if isinstance(updated, str):
                    current = updated
            except Exception:
                self.ctx.log.exception(f"Plugin process_text failed: {plugin.name}")
        return current

    async def fire_event(self, event_name: str, *args, **kwargs) -> None:
        for plugin in self.plugins:
            if event_name not in (getattr(plugin, "events", []) or []):
                continue
            try:
                await plugin.on_event(event_name, *args, **kwargs)
            except Exception:
                self.ctx.log.exception(f"Plugin event failed: {plugin.name}:{event_name}")

    def has_event(self, event_name: str) -> bool:
        for plugin in self.plugins:
            if event_name in (getattr(plugin, "events", []) or []):
                return True
        return False

    async def try_command(self, cmd: str, message, channel) -> bool:
        cmd_lower = (cmd or "").lower().strip()
        for plugin in self.plugins:
            for command in (getattr(plugin, "commands", []) or []):
                if not cmd_lower.startswith(command.lower()):
                    continue
                try:
                    handled = await plugin.handle_command(cmd, message, channel)
                    if handled:
                        return True
                except Exception:
                    self.ctx.log.exception(f"Plugin command failed: {plugin.name}:{command}")
                    return False
        return False

    async def teardown_all(self) -> None:
        for plugin in reversed(self.plugins):
            try:
                await plugin.teardown()
            except Exception:
                self.ctx.log.exception(f"Plugin teardown failed: {plugin.name}")


async def load_plugins(names: list[str], ctx: PluginContext) -> PluginManager:
    plugins: list[Plugin] = []
    for name in names:
        try:
            mod = importlib.import_module(f"plugins.{name}")
        except Exception as exc:
            ctx.log.warning(f"Skipping plugin '{name}' (import failed): {exc}")
            continue

        plugin = getattr(mod, "plugin", None)
        if plugin is None:
            factory = getattr(mod, "create_plugin", None)
            if callable(factory):
                try:
                    plugin = factory()
                except Exception as exc:
                    ctx.log.warning(f"Skipping plugin '{name}' (factory failed): {exc}")
                    continue
        if plugin is None:
            ctx.log.warning(f"Skipping plugin '{name}' (no 'plugin' or 'create_plugin')")
            continue

        try:
            await plugin.setup(ctx)
        except Exception as exc:
            ctx.log.warning(f"Skipping plugin '{name}' (setup failed): {exc}")
            continue
        plugins.append(plugin)

    return PluginManager(plugins, ctx)
