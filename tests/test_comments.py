import sys
import tempfile
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from pyrogram.errors import FloodWait
from bot.comments import collect_comment_media, downloadable_comment_kind


def msg(i, kind="photo", chat=-1002, mime=None, file_id="file"):
    return NS(id=i, chat=NS(id=chat), empty=False, caption=None,
              media=NS(value=kind), **{kind: NS(file_id=file_id, mime_type=mime)})


class Client:
    def __init__(self, replies):
        self.get_discussion_message = AsyncMock(return_value=msg(1))
        self.replies = replies
        self.calls = []

    async def get_discussion_replies(self, chat, root):
        self.calls.append((chat, root))
        for m in self.replies:
            if isinstance(m, Exception):
                raise m
            yield m


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_replies_and_progress(self):
        client = Client([msg(i) for i in range(252, 1, -1)])
        progress = AsyncMock()
        result = await collect_comment_media(client, "channel", 99, on_progress=progress)
        client.get_discussion_message.assert_awaited_once_with("channel", 99)
        self.assertEqual(client.calls, [(-1002, 1)])
        self.assertEqual(result.scanned, 251)
        self.assertEqual([m.id for m in result.messages], list(range(2, 253)))
        self.assertEqual(progress.await_count, 2)

    async def test_filter_and_deduplicate(self):
        client = Client([msg(10), msg(10), msg(9, "web_page"), msg(8, "poll"),
                         msg(7, file_id=None), msg(6, "document", mime="application/pdf"),
                         msg(5, "document", mime="image/png"),
                         msg(4, "document", mime="video/mp4"), msg(3, chat=-1003),
                         msg(2, "animation"), msg(1), None, NS(empty=True)])
        result = await collect_comment_media(client, "channel", 99)
        self.assertEqual([m.id for m in result.messages], [2, 4, 5, 10])
        self.assertEqual((result.photos, result.videos), (2, 2))
        self.assertEqual(result.scanned, 8)

    async def test_empty(self):
        result = await collect_comment_media(Client([]), "channel", 99)
        self.assertEqual(result.messages, [])

    async def test_pinned_sdk_paginates_more_than_100_replies(self):
        from pyrogram import Client as SDKClient
        from pyrogram.types import Message

        client = Client([])
        client.resolve_peer = AsyncMock(return_value=NS())
        offsets = []
        messages = [msg(i) for i in range(206, 1, -1)]

        async def invoke(request):
            offsets.append(request.add_offset)
            return NS(users=[], chats=[], messages=messages[request.add_offset:request.add_offset + request.limit])

        client.invoke = invoke
        client.get_discussion_replies = SDKClient.get_discussion_replies.__get__(client)
        with patch.object(Message, "_parse", AsyncMock(side_effect=lambda _, m, *args, **kwargs: m)):
            result = await collect_comment_media(client, "channel", 99)
        self.assertEqual(len(result.messages), 205)
        self.assertEqual(offsets, [0, 100, 200, 205])

    async def test_unavailable(self):
        client = Client([])
        client.get_discussion_message.return_value = NS(empty=True)
        with self.assertRaises(ValueError):
            await collect_comment_media(client, "channel", 99)

    async def test_partial_scan_error_propagates(self):
        with self.assertRaisesRegex(RuntimeError, "access denied"):
            await collect_comment_media(Client([msg(10), RuntimeError("access denied")]), "channel", 99)

    async def test_flood_wait_no_duplicate_or_new_replies(self):
        client = Client([msg(10), FloodWait(2)])

        async def resume(_):
            client.replies = [msg(11), msg(10), msg(9, "video")]

        with patch("bot.comments.asyncio.sleep", side_effect=resume) as sleep:
            result = await collect_comment_media(client, "channel", 99)
        sleep.assert_awaited_once_with(3)
        self.assertEqual([m.id for m in result.messages], [9, 10])
        self.assertEqual(result.scanned, 2)

    async def test_flood_wait_bounded(self):
        with patch("bot.comments.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(FloodWait):
                await collect_comment_media(Client([FloodWait(1)]), "channel", 99)
        self.assertEqual(sleep.await_count, 5)

    def test_media_requires_file_id(self):
        for kind in ["photo", "video", "animation", "live_photo"]:
            self.assertIsNotNone(downloadable_comment_kind(msg(2, kind)))
            self.assertIsNone(downloadable_comment_kind(msg(2, kind, file_id="")))


class QueueTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Use the real handler with an isolated app module: no login/network.
        cls.tmp = tempfile.TemporaryDirectory()
        app = types.ModuleType("bot.app")
        app.BASE_FOLDER = app.CONFIG_FOLDER = cls.tmp.name
        app.ADMINS, app.app, app.user = [], NS(), NS()
        previous = sys.modules.get("bot.app")
        sys.modules["bot.app"] = app
        try:
            from bot.download import handler
        finally:
            if previous is None:
                sys.modules.pop("bot.app", None)
            else:
                sys.modules["bot.app"] = previous
        cls.h = handler

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    async def test_comments_batch(self):
        h = self.h
        result = NS(messages=[msg(2)], scanned=3, photos=1, videos=0)
        with patch.object(h, "collect_comment_media", AsyncMock(return_value=result)), \
             patch.object(h, "enqueue_messages", AsyncMock()) as enqueue, \
             patch.object(h, "_status_message", AsyncMock()):
            await h.addCommentsFromLink(NS(), "channel", 99)
        self.assertTrue(enqueue.call_args.kwargs["force_batch"])
        self.assertEqual(enqueue.call_args.kwargs["override"], "评论_channel_99")
        self.assertTrue(enqueue.call_args.kwargs["batch_id"].startswith("comments:"))
        self.assertFalse(h._comment_scans)

    async def test_scan_error_enqueues_nothing(self):
        h = self.h
        with patch.object(h, "collect_comment_media", AsyncMock(side_effect=ValueError("private"))), \
             patch.object(h, "enqueue_messages", AsyncMock()) as enqueue, \
             patch.object(h, "_status_message", AsyncMock()) as status:
            with self.assertLogs(level="ERROR"):
                await h.addCommentsFromLink(NS(), "channel", 99)
        enqueue.assert_not_awaited()
        self.assertIn("未完成", status.call_args.args[1])
        self.assertFalse(h._comment_scans)

    async def test_empty_does_not_enqueue(self):
        h = self.h
        with patch.object(h, "collect_comment_media", AsyncMock(return_value=NS(messages=[], scanned=3))), \
             patch.object(h, "enqueue_messages", AsyncMock()) as enqueue, \
             patch.object(h, "_status_message", AsyncMock()):
            await h.addCommentsFromLink(NS(), "channel", 99)
        enqueue.assert_not_awaited()

    async def test_concurrent_scan_rejected(self):
        h = self.h
        h._comment_scans.add(("other", 1))
        try:
            with patch.object(h, "collect_comment_media", AsyncMock()) as collect, \
                 patch.object(h, "safe_reply", AsyncMock()) as reply:
                await h.addCommentsFromLink(NS(), "channel", 99)
            collect.assert_not_awaited()
            reply.assert_awaited_once()
        finally:
            h._comment_scans.clear()

    async def test_single_media_uses_folder_and_user_client(self):
        h = self.h
        with patch.object(h, "enqueue_file", AsyncMock()) as enqueue, \
             patch.object(h, "_status_message", AsyncMock()), \
             patch.object(h, "refresh_batch", AsyncMock()), \
             patch.object(h.queue_persist, "add_batch", Mock()):
            await h.enqueue_messages([msg(2)], h.user, reply_to=NS(), override="test-comments",
                                     force_batch=True, batch_id="comments:test")
        self.assertEqual(enqueue.call_args.kwargs["subfolder"], "test-comments")
        self.assertIs(enqueue.call_args.args[1], h.user)
        self.assertEqual(enqueue.call_args.kwargs["batch"].id, "comments:test")
        h.active_batches.pop("comments:test", None)

    async def test_range_batch_uses_own_folder(self):
        h = self.h
        result = NS(messages=[msg(2)], chat_id=-1002, start_id=2, end_id=10, total=9)
        with patch.object(h, "collect_range_media", AsyncMock(return_value=result)), \
             patch.object(h, "enqueue_messages", AsyncMock()) as enqueue, \
             patch.object(h, "_status_message", AsyncMock()):
            await h.addRangeFromLinks(NS(), ("channel", 2), ("channel", 10))
        self.assertTrue(enqueue.call_args.kwargs["force_batch"])
        self.assertEqual(enqueue.call_args.kwargs["override"], "范围_-1002_2-10")
        self.assertTrue(enqueue.call_args.kwargs["batch_id"].startswith("range:"))
        self.assertIs(enqueue.call_args.args[1], h.user)
        self.assertFalse(h._range_scanning)

    async def test_range_error_does_not_enqueue(self):
        h = self.h
        from bot.message_range import RangeInputError
        with patch.object(h, "collect_range_media", AsyncMock(side_effect=RangeInputError("必须同一个频道"))), \
             patch.object(h, "enqueue_messages", AsyncMock()) as enqueue, \
             patch.object(h, "_status_message", AsyncMock()) as status:
            await h.addRangeFromLinks(NS(), ("one", 2), ("two", 10))
        enqueue.assert_not_awaited()
        self.assertEqual(status.call_args.args[1], "必须同一个频道")
        self.assertFalse(h._range_scanning)

    async def test_range_empty_does_not_enqueue(self):
        h = self.h
        with patch.object(h, "collect_range_media", AsyncMock(return_value=NS(messages=[], total=9))), \
             patch.object(h, "enqueue_messages", AsyncMock()) as enqueue, \
             patch.object(h, "_status_message", AsyncMock()):
            await h.addRangeFromLinks(NS(), ("channel", 2), ("channel", 10))
        enqueue.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
