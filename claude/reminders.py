"""Reminder parsing and persistence for Claude bot."""

import json
import logging
import re
import subprocess
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REMINDER_ACTION_RE = re.compile(r"```reminder\s*\n(.*?)\n```", re.DOTALL)
PST = ZoneInfo("America/Los_Angeles")

_log = logging.getLogger(__name__)
_reminders_file = Path("selfbot/reminders.json")
_owner_id = 0
_create_flags = 0


def configure(reminders_file: Path, owner_id: int, create_flags: int, logger=None):
    global _reminders_file, _owner_id, _create_flags, _log
    _reminders_file = reminders_file
    _owner_id = owner_id
    _create_flags = create_flags
    if logger is not None:
        _log = logger


def load_reminders() -> list[dict]:
    if _reminders_file.exists():
        try:
            return json.loads(_reminders_file.read_text("utf-8"))
        except Exception:
            _log.warning("Corrupt reminders file")
    return []


def save_reminders(reminders: list[dict]):
    _reminders_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = _reminders_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(reminders, indent=2), "utf-8")
    tmp.replace(_reminders_file)


def _next_reminder_id(reminders: list[dict]) -> int:
    if not reminders:
        return 1
    return max(r.get("id", 0) for r in reminders) + 1


def process_reminder_actions(text: str, channel_id: int, channel_name: str, requester_id: int = 0) -> str:
    matches = list(REMINDER_ACTION_RE.finditer(text))
    if not matches:
        return text

    reminders = load_reminders()

    for m in matches:
        try:
            action = json.loads(m.group(1))
        except json.JSONDecodeError:
            _log.warning(f"Bad reminder JSON: {m.group(1)[:100]}")
            continue

        act = action.get("action")

        if act == "set":
            entry = {
                "id": _next_reminder_id(reminders),
                "text": action.get("text", ""),
                "time": action.get("time"),
                "channel_id": action.get("channel_id", channel_id),
                "created": datetime.now(PST).isoformat(),
                "source_channel": channel_name,
                "requester_id": requester_id or _owner_id,
                "fired": False,
            }
            reminders.append(entry)
            _log.info(f"Reminder set: #{entry['id']} - {entry['text'][:60]} @ {entry['time']}")

        elif act == "cancel":
            rid = action.get("id")
            before = len(reminders)
            reminders = [r for r in reminders if r.get("id") != rid]
            if len(reminders) < before:
                _log.info(f"Reminder cancelled: #{rid}")

    save_reminders(reminders)
    return REMINDER_ACTION_RE.sub("", text).strip()


def format_reminders_for_prompt() -> str:
    reminders = [r for r in load_reminders() if not r.get("fired")]
    if not reminders:
        return "(no pending reminders)"
    lines = []
    for r in reminders:
        lines.append(f"  #{r['id']} \"{r['text']}\" - fires at {r['time']} (set from {r.get('source_channel', '?')})")
    return "\n".join(lines)


def send_toast(title: str, message: str):
    try:
        ps_script = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] > $null
$template = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>{title.replace('"', '&quot;')}</text>
      <text>{message.replace('"', '&quot;')}</text>
    </binding>
  </visual>
  <audio src="ms-winsoundevent:Notification.Default"/>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("claudebot").Show($toast)
'''
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_script],
            creationflags=_create_flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        _log.warning(f"Toast notification failed: {exc}")


async def reminder_loop(client, home_channel_id: int, poll_seconds: int = 30):
    """Background loop that fires due reminders and posts pings to Discord."""
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            now = datetime.now(PST)
            reminders = load_reminders()
            changed = False

            for reminder in reminders:
                if reminder.get("fired"):
                    continue
                try:
                    fire_time = datetime.fromisoformat(reminder["time"])
                    if fire_time.tzinfo is None:
                        fire_time = fire_time.replace(tzinfo=PST)
                except (ValueError, KeyError):
                    continue

                if now >= fire_time:
                    reminder["fired"] = True
                    changed = True
                    _log.info(f"Firing reminder #{reminder['id']}: {reminder['text'][:60]}")
                    send_toast("Reminder", reminder["text"])

                    try:
                        target_ch_id = reminder.get("channel_id", home_channel_id)
                        channel = client.get_channel(target_ch_id)
                        if channel is None:
                            channel = await client.fetch_channel(target_ch_id)
                        source = reminder.get("source_channel", "?")
                        ping_id = reminder.get("requester_id", _owner_id)
                        await channel.send(f"<@{ping_id}> **Reminder** (from #{source}): {reminder['text']}")
                    except Exception as exc:
                        _log.warning(f"Failed to send reminder #{reminder['id']} to channel: {exc}")

            if changed:
                cutoff = now - timedelta(days=1)
                reminders = [
                    reminder
                    for reminder in reminders
                    if not reminder.get("fired")
                    or datetime.fromisoformat(reminder.get("created", now.isoformat())).replace(tzinfo=PST)
                    > cutoff
                ]
                save_reminders(reminders)

        except Exception:
            _log.exception("Reminder loop error")

        await asyncio.sleep(poll_seconds)
