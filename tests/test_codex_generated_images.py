import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex import bridge


class GeneratedImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_image_uploads_once_after_started(self):
        server = bridge.CodexAppServer()
        turn = bridge._TurnState()
        turn.on_image = AsyncMock()
        server._turns['thread'] = turn
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fly.png'
            path.write_bytes(b'image')
            item = dict(type='imageGeneration', id='image-1', status='completed',
                        savedPath=str(path), result='')
            params = dict(threadId='thread', item=item)
            await server._handle_notification('item/started', params)
            await server._handle_notification('item/completed', params)
            await server._handle_notification('item/completed', params)
            turn.on_image.assert_awaited_once_with(str(path))

    async def test_base64_fallback_and_thread_isolation(self):
        server = bridge.CodexAppServer()
        turn = bridge._TurnState()
        turn.on_image = AsyncMock()
        server._turns['thread'] = turn
        item = dict(type='imageGeneration', id='image-2', status='completed',
                    result=base64.b64encode(b'image bytes').decode())
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                'os.environ', CODEX_HOME=directory):
            await server._handle_notification('item/completed', dict(threadId='other', item=item))
            turn.on_image.assert_not_awaited()
            await server._handle_notification('item/completed', dict(threadId='thread', item=item))
            path = turn.on_image.await_args.args[0]
            self.assertEqual(Path(path).read_bytes(), b'image bytes')

    async def test_failed_image_is_not_uploaded(self):
        server = bridge.CodexAppServer()
        turn = bridge._TurnState()
        turn.on_image = AsyncMock()
        server._turns['thread'] = turn
        await server._handle_notification('item/completed', dict(threadId='thread', item=dict(
            type='imageGeneration', id='bad', status='failed', result='')))
        turn.on_image.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
