import unittest
from unittest.mock import AsyncMock, patch

from kimi.bridge import KimiBridge


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_cli_restarts_with_same_session(self):
        bridge = KimiBridge()
        old = AsyncMock()
        old.is_busy = False
        old.session_id = "saved-conversation"
        bridge._procs["room"] = old
        new = AsyncMock()
        with patch("kimi.bridge._PersistentProcess", return_value=new):
            result = await bridge.get_or_create("room", ".", "older-id")
        old.kill.assert_awaited_once()
        new.start.assert_awaited_once_with("saved-conversation")
        self.assertIs(result, new)

    async def test_active_turn_is_never_recycled(self):
        bridge = KimiBridge()
        active = AsyncMock()
        active.is_busy = True
        bridge._procs["room"] = active
        self.assertIs(await bridge.get_or_create("room", "."), active)
        active.kill.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
