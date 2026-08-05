"""lite-organizer 入口:加载配置 → 启动轮询线程 + HTTP 服务。

用法:
    python -m src.main --config /path/to/config.yaml
环境变量:
    LITE_CONFIG  配置文件路径(默认 ./config.yaml)
    LITE_TOKEN   覆盖 server.token
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time

from .api import ApiServer
from .config import load_config
from .engine import TransferEngine
from .history import HistoryStore
from .log import setup_logger

logger = logging.getLogger("lite-organizer")


def _poll_loop(engine: TransferEngine, downloader: str, interval: int, stop: threading.Event):
    """下载器轮询线程:对账「已完成未打标签」的任务。"""
    logger.info(f"轮询线程已启动: {downloader} 每 {interval}s")
    while not stop.is_set():
        try:
            engine.poll_once(downloader=downloader)
        except Exception as e:  # noqa: BLE001
            logger.error(f"轮询异常[{downloader}]: {e}")
        stop.wait(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="lite-organizer 轻量媒体整理服务")
    parser.add_argument("--config", default=os.getenv("LITE_CONFIG", "config.yaml"),
                        help="配置文件路径(默认: config.yaml 或 $LITE_CONFIG)")
    args = parser.parse_args()

    # 配置
    try:
        conf = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"配置加载失败: {e}")
        raise SystemExit(1)

    setup_logger(conf.log)
    logger.info("lite-organizer 启动中 ...")

    # 存储 + 引擎
    store = HistoryStore(conf.history.db, conf.history.keep_days)
    purged = store.purge()
    if purged:
        logger.info(f"已清理过期历史 {purged} 条")
    engine = TransferEngine(conf, store)

    # 轮询线程
    stop_event = threading.Event()
    pollers = []
    for dl in conf.downloaders:
        if dl.poll_interval > 0:
            t = threading.Thread(target=_poll_loop,
                                 args=(engine, dl.name, dl.poll_interval, stop_event),
                                 name=f"poll-{dl.name}", daemon=True)
            t.start()
            pollers.append(t)

    # HTTP 服务
    server = ApiServer(conf, engine)
    server_thread = threading.Thread(target=server.serve_forever,
                                     name="http", daemon=True)
    server_thread.start()

    def _shutdown(signum, frame):  # noqa: ARG001
        logger.info(f"收到信号 {signum},正在退出 ...")
        stop_event.set()
        server.shutdown()
        engine.close()
        store.close()
        logger.info("已退出")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("lite-organizer 启动完成")
    while not stop_event.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()
