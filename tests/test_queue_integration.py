import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch


class QueueIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        fake = types.ModuleType("bot.app")
        fake.BASE_FOLDER = fake.DL_FOLDER = fake.CONFIG_FOLDER = cls.tmp.name
        fake.ADMINS, fake.app, fake.user = [], NS(), NS()
        previous = sys.modules.get("bot.app")
        sys.modules["bot.app"] = fake
        try:
            from bot.download import batches, handler, panel, persist, render, restore, state
            from bot import commands
        finally:
            if previous is None:
                sys.modules.pop("bot.app", None)
            else:
                sys.modules["bot.app"] = previous
        cls.batches, cls.handler, cls.panel = batches, handler, panel
        cls.persist, cls.render, cls.restore, cls.state = persist, render, restore, state
        cls.commands = commands
        from bot.download.types import Batch, BatchItem, Download, allocate_download_id
        cls.Batch, cls.Item, cls.Download = Batch, BatchItem, Download
        cls.allocate_id = staticmethod(allocate_download_id)
        with patch.dict(os.environ, {"CONFIG_FOLDER": cls.tmp.name, "DOWNLOAD_FOLDER": cls.tmp.name}):
            from web import server
        cls.server = server

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.state.active_batches.clear()
        self.state.downloads.clear()
        self.state.active_downloads.clear()
        self.state.rename_targets.clear()
        self.state.holds.clear()
        self.state.stop.clear()
        self.state.paused = False

    def tearDown(self):
        self.setUp()

    def source(self, chat=-1002, identifier=10):
        return NS(id=identifier, chat=NS(id=chat), empty=False, media=NS(value="photo"),
                  photo=NS(file_id="file", file_size=100, file_unique_id=""), caption=None)

    def test_distinct_tasks_and_original_source_persisted(self):
        h = self.handler
        a = h._make_download(self.source(-1002), h.user, "a.jpg", None, 100, "", None, None)
        b = h._make_download(self.source(-1003), h.user, "b.jpg", None, 100, "", None, None)
        self.assertNotEqual(a.id, b.id)
        self.assertLess(a.id, 2**53)
        self.state.active_downloads.extend([a, b])
        self.assertIs(self.state.find_download_by_id(b.id), b)
        self.assertEqual(self.persist.task_record(a)["message_id"], 10)
        self.assertEqual(self.persist.task_record(a)["key"], "-1002:10")
        self.assertEqual(self.persist.task_record(a)["task_id"], a.id)

    async def test_other_chat_same_message_id_not_skipped(self):
        h = self.handler
        existing = h._make_download(self.source(-1002), h.user, "a.jpg", None, 100, "", None, None)
        self.state.active_downloads.append(existing)
        batch = self.Batch("test", "folder", 1, None)
        with patch.object(h, "is_unique_duplicate", return_value=False), \
             patch.object(h, "queue_download", Mock()) as queue:
            await h.enqueue_file(self.source(-1003), h.user, batch=batch, subfolder="folder")
        queue.assert_called_once()
        self.assertNotEqual(queue.call_args.args[0].id, existing.id)

    async def test_duplicate_source_is_counted_as_skipped(self):
        h = self.handler
        existing = h._make_download(self.source(), h.user, "a.jpg", None, 100, "", None, None)
        self.state.active_downloads.append(existing)
        batch = self.Batch("test", "folder", 1, None)
        with patch.object(h, "queue_download", Mock()) as queue:
            await h.enqueue_file(self.source(), h.user, batch=batch, subfolder="folder")
        queue.assert_not_called()
        self.assertEqual(batch.skipped, 1)

    async def test_building_batch_cannot_finish_early(self):
        batch = self.Batch("comments:test", "folder", 20, None, building=True)
        batch.items.append(self.Item(1, "one.jpg", status="done"))
        self.state.active_batches[batch.id] = batch
        with patch.object(self.batches, "safe_edit", AsyncMock()), \
             patch.object(self.persist, "remove_batch", Mock()) as remove:
            await self.batches.finish_batch_item(batch)
            self.assertIn(batch.id, self.state.active_batches)
            remove.assert_not_called()
            self.assertEqual(self.panel.snapshot()["items"][0]["count"], 20)
            batch.building = False
            await self.batches.finish_batch_item(batch)
            remove.assert_called_once_with(batch.id)

    async def test_enqueue_keeps_batch_building_until_all_items_added(self):
        h = self.handler
        seen = []

        async def enqueue(message, client, **kwargs):
            batch = kwargs["batch"]
            seen.append(batch.building)
            batch.items.append(self.Item(message.id, "file.jpg", status="done"))
            await self.batches.finish_batch_item(batch)
            self.assertIn(batch.id, self.state.active_batches)

        with patch.object(h, "enqueue_file", side_effect=enqueue), \
             patch.object(h, "_status_message", AsyncMock()), \
             patch.object(self.batches, "safe_edit", AsyncMock()), \
             patch.object(self.persist, "add_batch", Mock()), \
             patch.object(self.persist, "remove_batch", Mock()):
            await h.enqueue_messages([self.source(identifier=1), self.source(identifier=2)], h.user,
                                     force_batch=True, batch_id="range:test", override="folder")
        self.assertEqual(seen, [True, True])
        self.assertNotIn("range:test", self.state.active_batches)

    def test_large_telegram_batch_message_is_bounded(self):
        batch = self.Batch("range:test", "folder", 500, None)
        batch.items = [self.Item(i, "图片😀" * 40 + f"{i}.jpg") for i in range(500)]
        batch.items[-1].status = "downloading"
        text = self.render.render_batch(batch)
        self.assertLess(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertIn("另有", text)
        self.assertIn("499.jpg", text)

    def test_comments_and_range_web_snapshot_contract(self):
        for prefix in ["comments", "range"]:
            batch = self.Batch(f"{prefix}:test", f"{prefix}-folder", 20, None)
            batch.items = [self.Item(i, f"file{i}.jpg", status="done" if i < 5 else "waiting") for i in range(20)]
            self.state.active_batches[batch.id] = batch
        snapshot = self.panel.snapshot()
        self.assertEqual(snapshot["total"], 2)
        self.assertTrue(all(item["done"] == 5 and item["count"] == 20 for item in snapshot["items"]))
        for item in snapshot["items"]:
            self.assertEqual(item["kind"], "batch")
            self.assertEqual(item["batch_id"], item["id"])
        with self.server.app.test_client() as client, \
             patch.object(self.server, "authorized", return_value=True), \
             patch.object(self.server.panel_proxy, "call", return_value=(snapshot, 200)) as call:
            response = client.get("/api/downloads")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["items"], snapshot["items"])
            response = client.post("/api/downloads/batch/comments%3Atest/stop")
            self.assertEqual(response.status_code, 200)
            call.assert_called_with("stop_batch", id="comments:test")

    async def test_web_pause_resume_and_batch_stop(self):
        batch = self.Batch("range:test", "folder", 1, None)
        batch.items = [self.Item(1, "file.jpg")]
        self.state.active_batches[batch.id] = batch
        self.assertTrue(self.panel.set_paused(True)["paused"])
        self.assertEqual(self.panel.snapshot()["items"][0]["phase"], "paused")
        self.assertFalse(self.panel.set_paused(False)["paused"])
        with patch.object(self.panel, "schedule_stop_batch", side_effect=lambda b: setattr(b, "stopped", True)) as stop:
            response = await self.panel.stop_batch(batch.id)
        stop.assert_called_once_with(batch)
        self.assertEqual(response["total"], 0)

    def test_web_queue_requires_auth(self):
        with self.server.app.test_client() as client, \
             patch.object(self.server, "authorized", return_value=False), \
             patch.object(self.server.panel_proxy, "call") as call:
            self.assertEqual(client.get("/api/downloads").status_code, 401)
            self.assertEqual(client.post("/api/downloads/batch/range%3Atest/stop").status_code, 401)
        call.assert_not_called()

    async def test_range_command_and_menu(self):
        c = self.commands
        with patch.object(c.download_handler, "addRangeFromLinks", AsyncMock()) as add:
            await c.downloadRange(None, NS(text="/range https://t.me/testchannel/10 https://t.me/testchannel/20 我的 收藏"))
        self.assertEqual(add.call_args.args[1:], (("testchannel", 10), ("testchannel", 20)))
        self.assertEqual(add.call_args.kwargs["folder_name"], "我的 收藏")
        client = NS(set_bot_commands=AsyncMock())
        await c.set_menu(client)
        names = [item.command for item in client.set_bot_commands.call_args.args[0]]
        self.assertIn("comments", names)
        self.assertIn("range", names)

    async def test_restore_fetches_source_id_not_task_id(self):
        source = self.source()
        client = NS(get_messages=AsyncMock(return_value=[source]))
        record = {"filename": "new.jpg", "chat_id": -1002, "message_id": 10,
                  "client": "user", "task_id": self.allocate_id(),
                  "old_progress": {"chat_id": 123, "message_id": 456}}
        # The allocator reserves existing IDs, so restoration also repairs collisions.
        with patch.object(self.restore, "user", client), \
             patch.object(self.restore, "BASE_FOLDER", self.tmp.name), \
             patch.object(self.restore, "queue_download", Mock()) as queue, \
             patch.object(self.restore, "send_restore_message", AsyncMock()):
            outcome = await self.restore._restore_task("-1002:10", record, {})
        self.assertEqual(outcome, self.restore.RestoreOutcome.QUEUED)
        client.get_messages.assert_awaited_once_with(-1002, [10])
        restored = queue.call_args.args[0]
        self.assertEqual(self.persist.task_record(restored)["message_id"], 10)
        self.assertNotEqual(restored.id, record["task_id"])

    async def test_retry_refresh_uses_source_message_id(self):
        from bot.download.lifecycle import refetch_source_message
        source = self.source()
        client = NS(get_messages=AsyncMock(return_value=[source]))
        download = self.handler._make_download(source, client, "file.jpg", None, 100, "", None, None)
        result = await refetch_source_message(download)
        client.get_messages.assert_awaited_once_with(-1002, [10])
        self.assertIs(result, source)
