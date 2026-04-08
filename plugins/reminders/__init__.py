from claude.reminders import REMINDER_ACTION_RE
from claude.reminders import configure as configure_reminders
from claude.reminders import process_reminder_actions
from claude.reminders import reminder_loop
from shared.plugin import Plugin
from shared.plugin import PluginContext


class RemindersPlugin(Plugin):
    name = "reminders"

    async def setup(self, ctx: PluginContext) -> None:
        reminders_file = ctx.project_root / "selfbot" / "reminders.json"
        create_flags = int(ctx.extra.get("create_flags", 0))
        configure_reminders(reminders_file, ctx.owner_id, create_flags, ctx.log)
        if not getattr(ctx.client, "_reminder_task_started", False):
            ctx.client._reminder_task_started = True
            home_channel_id = int(ctx.extra.get("home_channel_id", 0))
            ctx.register_task(reminder_loop(ctx.client, home_channel_id))

    def strip_text(self, text: str, **kwargs) -> str:
        return REMINDER_ACTION_RE.sub("", text)

    async def process_text(self, text: str, **kwargs) -> str:
        if not text:
            return text
        channel_id = int(kwargs.get("channel_id", 0) or 0)
        channel_name = str(kwargs.get("channel_name") or "?")
        requester_id = int(kwargs.get("requester_id", 0) or 0)
        return process_reminder_actions(text, channel_id, channel_name, requester_id)


plugin = RemindersPlugin()
