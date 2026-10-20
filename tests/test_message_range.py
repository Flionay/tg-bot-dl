import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from pyrogram.errors import FloodWait

from bot.message_range import RangeInputError, collect_range_media


def message(i, kind="photo", *, chat=-1002, file_id="file"):
    return NS(id=i, chat=NS(id=chat), empty=False, media=NS(value=kind),
              **{kind: NS(file_id=file_id)})


def client_with(messages):
    client = NS(get_chat=AsyncMock(return_value=NS(id=-1002)))

    async def get_messages(chat, ids, replies=0):
        return [m for m in messages if m is None or getattr(m, "empty", False) or m.id in ids]

    client.get_messages = AsyncMock(side_effect=get_messages)
    return client


class RangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_inclusive_bounds_and_pages(self):
        client = client_with([message(i) for i in range(5, 211)])
        progress = AsyncMock()
        result = await collect_range_media(client, ("channel", 5), ("channel", 210), on_progress=progress)
        self.assertEqual([m.id for m in result.messages], list(range(5, 211)))
        self.assertEqual((result.scanned, result.total), (206, 206))
        calls = client.get_messages.await_args_list
        self.assertEqual([len(c.args[1]) for c in calls], [100, 100, 6])
        self.assertTrue(all(c.kwargs["replies"] == 0 for c in calls))
        self.assertEqual(progress.await_count, 3)

    async def test_reversed_and_single_message_ranges(self):
        client = client_with([message(10), message(11)])
        result = await collect_range_media(client, ("channel", 11), ("channel", 10))
        self.assertEqual([m.id for m in result.messages], [10, 11])
        result = await collect_range_media(client, ("channel", 10), ("channel", 10))
        self.assertEqual([m.id for m in result.messages], [10])

    async def test_aliases_of_same_chat(self):
        client = client_with([message(10)])
        result = await collect_range_media(client, ("channel", 10), (-1002, 10))
        self.assertEqual(result.chat_id, -1002)
        self.assertEqual(client.get_chat.await_count, 2)

    async def test_different_chats_rejected_before_read(self):
        client = client_with([])
        client.get_chat.side_effect = [NS(id=-1002), NS(id=-1003)]
        with self.assertRaisesRegex(RangeInputError, "同一个"):
            await collect_range_media(client, ("one", 1), ("two", 99))
        client.get_messages.assert_not_awaited()

    async def test_deleted_text_and_non_file_media_skipped(self):
        messages = [None, NS(empty=True), message(1, "poll"), message(2, "web_page"),
                    message(3, file_id=None), message(4, chat=-1003),
                    message(5, "document"), message(6, "video"), message(6, "video"),
                    NS(id=7, empty=False, chat=NS(id=-1002), media=None)]
        client = client_with(messages)
        result = await collect_range_media(client, ("channel", 1), ("channel", 7))
        self.assertEqual([m.id for m in result.messages], [5, 6])

    async def test_supported_file_kinds(self):
        kinds = ["photo", "video", "document", "animation", "live_photo", "audio", "voice", "video_note", "sticker"]
        result = await collect_range_media(client_with([message(i + 1, k) for i, k in enumerate(kinds)]),
                                           ("channel", 1), ("channel", 9))
        self.assertEqual(len(result.messages), 9)

    async def test_out_of_range_album_items_not_included(self):
        client = client_with([])
        client.get_messages.return_value = None
        client.get_messages.side_effect = None
        client.get_messages.return_value = [message(9), message(10), message(11), message(12)]
        result = await collect_range_media(client, ("channel", 10), ("channel", 11))
        self.assertEqual([m.id for m in result.messages], [10, 11])

    async def test_invalid_ids(self):
        for identifier in [0, -1, 2**31]:
            with self.subTest(identifier=identifier):
                client = client_with([])
                with self.assertRaises(RangeInputError):
                    await collect_range_media(client, ("channel", identifier), ("channel", 10))
                client.get_chat.assert_not_awaited()

    async def test_failed_page_does_not_return_partial_success(self):
        client = client_with([])
        client.get_messages.side_effect = [[message(1)], RuntimeError("network")]
        with self.assertRaisesRegex(RuntimeError, "network"):
            await collect_range_media(client, ("channel", 1), ("channel", 101))

    async def test_flood_wait_retries_same_page(self):
        client = client_with([])
        client.get_messages.side_effect = [FloodWait(2), [message(10)]]
        with patch("bot.message_range.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await collect_range_media(client, ("channel", 10), ("channel", 10))
        sleep.assert_awaited_once_with(3)
        self.assertEqual([m.id for m in result.messages], [10])
        self.assertEqual(client.get_messages.await_args_list[0], client.get_messages.await_args_list[1])

    async def test_flood_wait_bounded(self):
        client = client_with([])
        client.get_messages.side_effect = FloodWait(1)
        with patch("bot.message_range.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(FloodWait):
                await collect_range_media(client, ("channel", 1), ("channel", 1))
        self.assertEqual(sleep.await_count, 5)

    async def test_invalid_response_is_failure(self):
        client = client_with([])
        client.get_messages.side_effect = None
        client.get_messages.return_value = None
        with self.assertRaises(RuntimeError):
            await collect_range_media(client, ("channel", 1), ("channel", 1))
