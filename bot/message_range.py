"""Collect downloadable media in an inclusive message ID range."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pyrogram.errors import FloodWait


class RangeInputError(ValueError):
    pass


@dataclass
class RangeMedia:
    chat_id: int
    start_id: int
    end_id: int
    messages: list = field(default_factory=list)
    scanned: int = 0

    @property
    def total(self):
        return self.end_id - self.start_id + 1


def is_downloadable(message):
    kind = getattr(getattr(message, "media", None), "value", None)
    if kind not in {
        "photo", "video", "animation", "live_photo", "document",
        "audio", "voice", "video_note", "sticker",
    }:
        return False
    return bool(getattr(getattr(message, kind, None), "file_id", None))


async def _read_page(client, chat_id, ids):
    for attempt in range(6):
        try:
            return await client.get_messages(chat_id, ids, replies=0)
        except FloodWait as exc:
            if attempt == 5:
                raise
            await asyncio.sleep(max(1, int(exc.value)) + 1)


async def collect_range_media(client, start, end, *, on_progress=None):
    """Resolve both peers, then read IDs in pages of 100 without expanding albums.

    Bounds are inclusive and order-independent. A missing/deleted message is
    skipped, but a failed page propagates instead of returning a partial scan.
    """
    for _, identifier in (start, end):
        if not 0 < identifier <= 2**31 - 1:
            raise RangeInputError("消息编号必须是 1 到 2147483647 之间的整数。")
    first = await client.get_chat(start[0])
    last = first if start[0] == end[0] else await client.get_chat(end[0])
    if first is None or last is None:
        raise RangeInputError("无法读取链接对应的频道或群组，请检查用户账号的访问权限。")
    if first.id != last.id:
        raise RangeInputError("起始和结束链接必须属于同一个频道或群组。")
    lower, upper = sorted((start[1], end[1]))
    result = RangeMedia(chat_id=first.id, start_id=lower, end_id=upper)
    seen = set()
    for offset in range(lower, upper + 1, 100):
        ids = list(range(offset, min(offset + 100, upper + 1)))
        page = await _read_page(client, first.id, ids)
        if page is None:
            # A list request must return a list (possibly containing empty
            # messages). Treat an invalid response as failure, not completion.
            raise RuntimeError("读取消息范围时未返回有效结果")
        for message in page:
            if not message or getattr(message, "empty", False):
                continue
            chat = getattr(message, "chat", None)
            if not chat or chat.id != first.id or message.id not in ids or message.id in seen:
                continue
            seen.add(message.id)
            if is_downloadable(message):
                result.messages.append(message)
        result.scanned += len(ids)
        if on_progress:
            await on_progress(result)
    result.messages.sort(key=lambda message: message.id)
    return result
