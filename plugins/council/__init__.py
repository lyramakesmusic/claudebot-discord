from integrations.council import call_gpt
from integrations.council import call_researcher
from shared.discord_utils import split_message
from shared.plugin import Plugin


class CouncilPlugin(Plugin):
    name = "council"
    actions = ["call_gpt", "call_researcher"]

    def __init__(self):
        self._gpt_history: dict[int, list[dict]] = {}

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        action_name = str(action.get("action", "")).strip()
        if action_name == "call_gpt":
            return await self._handle_call_gpt(action, channel)
        if action_name == "call_researcher":
            return await self._handle_call_researcher(action, channel)
        return {"results": []}

    async def _handle_call_gpt(self, action: dict, channel) -> dict:
        gpt_msg = str(action.get("message", "")).strip()
        if not gpt_msg:
            return {"results": ["call_gpt: no message given"]}

        for chunk in split_message(f"**[to GPT]** {gpt_msg}"):
            try:
                await channel.send(chunk)
            except Exception:
                pass

        ch_id = channel.id
        history = self._gpt_history.setdefault(ch_id, [])
        history.append({"role": "user", "content": gpt_msg})

        gpt_result = await call_gpt(history)
        if gpt_result["error"]:
            try:
                await channel.send(f"**[GPT error]** {gpt_result['error'][:500]}")
            except Exception:
                pass
            return {"results": [f"GPT error: {gpt_result['error'][:200]}"]}

        gpt_content = gpt_result["content"]
        history.append({"role": "assistant", "content": gpt_content})
        cost_note = f"\n-# Cost: ${gpt_result['cost']:.4f}" if gpt_result["cost"] else ""
        for chunk in split_message(f"**[GPT to Opus]** {gpt_content}{cost_note}"):
            try:
                await channel.send(chunk)
            except Exception:
                pass
        return {
            "results": [f"[GPT responded - {len(gpt_content)} chars]"],
            "council_feedback": [f"[GPT to Opus] {gpt_content}"],
        }

    async def _handle_call_researcher(self, action: dict, channel) -> dict:
        query = str(action.get("query", "")).strip()
        if not query:
            return {"results": ["call_researcher: no query given"]}

        context = str(action.get("context", "")).strip()
        try:
            await channel.send(f"**[Research query]** {query[:500]}")
        except Exception:
            pass

        research_result = await call_researcher(query, context)
        if research_result["error"]:
            try:
                await channel.send(f"**[Research error]** {research_result['error'][:500]}")
            except Exception:
                pass
            return {"results": [f"Research error: {research_result['error'][:200]}"]}

        research_content = research_result["content"]
        cost_note = f"\n-# Cost: ${research_result['cost']:.4f}" if research_result["cost"] else ""
        for chunk in split_message(f"**[Gemini - deep research]** {research_content}{cost_note}"):
            try:
                await channel.send(chunk)
            except Exception:
                pass
        return {
            "results": [f"[Research complete - {len(research_content)} chars]"],
            "council_feedback": [f"[Gemini - deep research] {research_content}"],
        }


plugin = CouncilPlugin()
