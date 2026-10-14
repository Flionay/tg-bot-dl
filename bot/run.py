import asyncio
import logging
from datetime import datetime

from pyrogram import idle
from pyrogram.errors import FloodWait

from bot.app import app, user
from bot import commands, listener, notify
from bot.download import lifecycle, restore
from bot.download import manager as download_manager
from bot import panel_sock

# 会话健康检查：连续失败达到阈值就重启连接。
# 只重启连接、不动进程，下载队列和任务都还在内存里，传输会自动断点续传。
SESSION_CHECK_INTERVAL = 60
SESSION_CHECK_TIMEOUT = 30
SESSION_FAIL_THRESHOLD = 3


def _handler_count(client) -> int:
    groups = getattr(getattr(client, "dispatcher", None), "groups", None) or {}
    return sum(len(items) for items in groups.values())


def _dispatcher_alive(client) -> bool:
    if client is None:
        return True
    tasks = getattr(getattr(client, "dispatcher", None), "handler_worker_tasks", None) or []
    return bool(tasks) and not all(task.done() for task in tasks)


def _reset_dispatcher_workers(client) -> None:
    """stop(clear_handlers=False) 会留下已结束的 worker/lock，start() 只会往列表里追加。"""
    dispatcher = getattr(client, "dispatcher", None)
    if dispatcher is None:
        return
    dispatcher.handler_worker_tasks.clear()
    dispatcher.locks_list.clear()


async def _reconnect(client, name: str) -> bool:
    try:
        await client.stop(clear_handlers=False)
    except Exception:
        logging.debug("停止%s失败", name, exc_info=True)
    _reset_dispatcher_workers(client)
    await client.start()
    empty = _handler_count(client) == 0
    if empty:
        logging.error("重连后%s处理器为空", name)
    return empty


async def restart_clients():
    logging.warning("Telegram 会话疑似假死，正在重启连接，进程和下载队列保持不动...")
    # 默认 stop() 会清空处理器；重连后 get_me 仍成功，但消息再也不会进业务回调
    bot_empty = await _reconnect(app, "机器人")
    if bot_empty:
        logging.error("机器人命令处理器丢失，本次无法自动补注册，需要重启进程")
    if user:
        user_empty = await _reconnect(user, "用户账号")
        if user_empty:
            logging.warning("重连后用户账号处理器为空，重新注册频道监听")
            listener.register_user_channel_handler(user)
    logging.info("Telegram 连接已恢复，下载任务会自动续传")


async def session_monitor():
    failures = 0
    while True:
        await asyncio.sleep(SESSION_CHECK_INTERVAL)
        try:
            await asyncio.wait_for(app.get_me(), timeout=SESSION_CHECK_TIMEOUT)
            if user:
                # 用户账号承担收帖和下载，会话假死同样要发现并重启
                await asyncio.wait_for(user.get_me(), timeout=SESSION_CHECK_TIMEOUT)
            if not _dispatcher_alive(app) or not _dispatcher_alive(user):
                raise RuntimeError("消息分发任务已退出")
            # 连接仍通时刷新入站时间，避免 Pyrogram 每 15 分钟对所有监听频道
            # 跑 GetChannelDifference，把会话堵死后再误判假死。
            now = datetime.now()
            app.last_update_time = now
            if user:
                user.last_update_time = now
            failures = 0
            # 连接正常就唤醒冷驻留任务，自动重试轮次用尽后保留断点等在这里
            try:
                await lifecycle.wake_cold_holds()
            except Exception:
                logging.exception("唤醒冷驻留下载任务失败")
            continue
        except FloodWait:
            failures = 0  # 能收到限流响应，说明连接本身是通的
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            logging.warning(
                "会话健康检查失败（%d/%d）",
                failures,
                SESSION_FAIL_THRESHOLD,
                exc_info=True,
            )
        if failures >= SESSION_FAIL_THRESHOLD:
            try:
                await restart_clients()
            except Exception:
                logging.exception("自动重启连接失败，下一轮继续尝试")
                continue
            failures = 0


async def main():
    logging.info("Registering commands...")
    commands.register(app)
    logging.info("Starting bot...")
    await app.start()
    await commands.set_menu(app)
    if user:
        logging.info("Starting normal user")
        await user.start()
        listener.register_user_channel_handler(user)
    logging.info("Restoring saved download queue...")
    try:
        await restore.restore_saved_queue()
    except Exception:
        logging.exception("恢复上次下载队列失败")
    logging.info("Starting download manager...")
    manager = asyncio.create_task(download_manager.run())
    panel = asyncio.create_task(panel_sock.serve())
    try:
        await notify.notify_version_update()
    except Exception:
        logging.exception("版本更新提示流程失败")
    monitor = asyncio.create_task(session_monitor())
    me = await app.get_me()
    logging.info("Bot started! I'm @%s", me.username)
    await idle()
    logging.info("Stopping download manager...")
    monitor.cancel()
    panel.cancel()
    manager.cancel()
    try:
        await panel
    except asyncio.CancelledError:
        pass
    logging.info("Stopping bot...")
    await app.stop()
    if user:
        logging.info("Stopping user...")
        await user.stop()
    logging.info("All systems stopped!")
    return 0


if __name__ == "__main__":
    event_loop = asyncio.get_event_loop()
    event_loop.run_until_complete(main())
