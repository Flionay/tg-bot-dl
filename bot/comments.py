"""Read one post's discussion; never scan the whole linked group."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pyrogram.errors import FloodWait


@dataclass
class CommentMedia:
    messages: list = field(default_factory=list)
    scanned: int = 0
    photos: int = 0
    videos: int = 0


def downloadable_comment_kind(message) -> str | None:
    """Only actual photo/video files, including ones uploaded as documents."""
    kind = getattr(getattr(message, "media", None), "value", None)
    if kind not in {"photo", "video", "animation", "live_photo", "document"}:
        return None
    media = getattr(message, kind, None)
    if not getattr(media, "file_id", None):
        return None
    if kind == "document":
        mime = str(getattr(media, "mime_type", "") or "").lower()
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("video/"):
            return "video"
        return None
    return "photo" if kind == "photo" else "video"


async def collect_comment_media(client, chat_id, message_id, *, on_progress=None):
    """Resolve the discussion root and enumerate all replies (SDK paginates).

    FloodWait can interrupt the SDK's iterator mid-page. Restart enumeration
    with message deduplication, rather than silently returning partial results.
    Other errors propagate: callers must not report a failed scan as complete.
    """
    root = await client.get_discussion_message(chat_id, message_id)
    if not root or getattr(root, "empty", False) or not getattr(root, "chat", None):
        raise ValueError("这条帖子没有可访问的评论区")
    result = CommentMedia()
    seen = set()
    ceiling = None
    flood_retries = 0
    while True:
        try:
            async for message in client.get_discussion_replies(root.chat.id, root.id):
                if not message or getattr(message, "empty", False):
                    continue
                chat = getattr(message, "chat", None)
                if not chat or chat.id != root.chat.id or message.id == root.id:
                    continue
                # Replies are newest-first. Ignore replies arriving after the
                # first result of this scan, including after a FloodWait restart.
                if ceiling is None:
                    ceiling = message.id
                if message.id > ceiling:
                    continue
                key = (chat.id, message.id)
                if key in seen:
                    continue
                seen.add(key)
                result.scanned += 1
                kind = downloadable_comment_kind(message)
                if kind:
                    result.messages.append(message)
                    if kind == "photo":
                        result.photos += 1
                    else:
                        result.videos += 1
                if on_progress and result.scanned % 100 == 0:
                    await on_progress(result)
            break
        except FloodWait as exc:
            flood_retries += 1
            if flood_retries > 5:
                raise
            await asyncio.sleep(max(1, int(exc.value)) + 1)
    result.messages.sort(key=lambda message: message.id)
    return result
