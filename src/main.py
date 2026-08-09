"""ptpilot 入口:加载配置 → 启动轮询线程 + HTTP 服务。

用法:
    python -m src.main --config /path/to/config.yaml
环境变量:
    PTPILOT_CONFIG            配置文件路径(默认 ./config.yaml)
    PTPILOT_TOKEN             覆盖 server.token
    PTPILOT_WATCH_INTERVAL    配置热重载监听间隔秒数(默认 3,0=关闭监听)
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
from .downloaders import create_adapter
from .history import HistoryStore
from .log import setup_logger
from .reload import ConfigManager
from .reseed.engine import ReseedEngine
from .reseed.matcher import JackettMatcher
from .reseed.store import ReseedStore
from .transfer.engine import TransferEngine

logger = logging.getLogger("ptpilot")


def _apply_uid_gid() -> None:
    """
    按环境变量 PUID/PGID 降权运行(参照 MoviePilot)。
    - 值 <= 0 或未设置:保持当前用户(容器默认 root)
    - 非 root 启动:跳过(无权限切换)
    """
    import grp  # noqa: PLC0415
    import os as _os  # noqa: PLC0415
    import pwd  # noqa: PLC0415

    if _os.geteuid() != 0:
        logger.info(f"非 root 启动,跳过 PUID/PGID 切换(当前 uid={_os.geteuid()})")
        return
    try:
        puid = int(_os.getenv("PUID", "0") or "0")
        pgid = int(_os.getenv("PGID", "0") or "0")
    except ValueError:
        logger.warning("PUID/PGID 非数字,忽略")
        return
    if puid <= 0 and pgid <= 0:
        logger.info("PUID/PGID 为 0,以 root 运行")
        return
    try:
        if pgid > 0:
            _os.setgid(pgid)
        if puid > 0:
            _os.setuid(puid)
        user = pwd.getpwuid(puid).pw_name if puid > 0 else "root"
        logger.info(f"已切换运行用户: {user} (uid={puid}, gid={pgid})")
    except (PermissionError, KeyError, OSError) as e:
        logger.warning(f"PUID/PGID 切换失败,继续以当前用户运行: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ptpilot 轻量媒体整理服务")
    parser.add_argument("--config", default=os.getenv("PTPILOT_CONFIG", "config.yaml"),
                        help="配置文件路径(默认: config.yaml 或 $PTPILOT_CONFIG)")
    args = parser.parse_args()

    # 配置
    try:
        conf = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"配置加载失败: {e}")
        raise SystemExit(1)

    setup_logger(conf.log)
    logger.info("ptpilot 启动中 ...")
    _apply_uid_gid()

    # 存储 + 引擎
    store = HistoryStore(conf.history.db, conf.history.keep_days)
    purged = store.purge()
    if purged:
        logger.info(f"已清理过期历史 {purged} 条")
    from .engine import TransferEngine as OrganizeEngine
    engine = OrganizeEngine(conf, store)

    # 下载器适配器(按名索引,转移/辅种共用)
    adapters = {}
    for dl in conf.downloaders:
        try:
            adapters[dl.name] = create_adapter(dl)
        except Exception as e:  # noqa: BLE001
            logger.error(f"创建下载器适配器失败 [{dl.name}]: {e}")

    # 转移引擎
    transfer_engine = None
    if conf.transfer.enabled:
        from_adapter = adapters.get(conf.transfer.from_client)
        to_adapter = adapters.get(conf.transfer.to_client)
        if from_adapter is None or to_adapter is None:
            logger.error("transfer 模块启用但下载器适配器创建失败,转移功能不可用")
        else:
            transfer_engine = TransferEngine(conf.transfer, from_adapter, to_adapter)
            logger.info(f"转移引擎已初始化: {conf.transfer.from_client} → {conf.transfer.to_client}")

    # 辅种引擎
    reseed_engine = None
    reseed_store = None
    if conf.reseed.enabled:
        if conf.reseed.target_client not in adapters:
            logger.error("reseed 模块启用但目标下载器适配器创建失败,辅种功能不可用")
        else:
            reseed_store = ReseedStore(conf.history.db)
            reseed_engine = ReseedEngine(
                conf.reseed, adapters, reseed_store,
                JackettMatcher(conf.reseed.jackett),
            )
            logger.info(f"辅种引擎已初始化: 注入目标={conf.reseed.target_client}")

    # 配置热重载管理器(轮询线程统一由它管理,支持文件监听 + 手动触发)
    stop_event = threading.Event()
    try:
        watch_interval = float(os.getenv("PTPILOT_WATCH_INTERVAL", "3"))
    except ValueError:
        watch_interval = 3.0
    runtime = {
        "conf": conf,
        "store": store,
        "engine": engine,
        "adapters": adapters,
        "transfer_engine": transfer_engine,
        "reseed_engine": reseed_engine,
        "reseed_store": reseed_store,
        "server": None,
        "pollers": {"organize": {}, "transfer": None, "reseed": None},
    }
    reload_manager = ConfigManager(args.config, runtime, watch_interval=watch_interval)

    # HTTP 服务
    server = ApiServer(conf, engine, transfer_engine=transfer_engine,
                       reseed_engine=reseed_engine, reload_manager=reload_manager)
    runtime["server"] = server
    server_thread = threading.Thread(target=server.serve_forever,
                                     name="http", daemon=True)
    server_thread.start()

    # 启动全部轮询线程 + 配置监听
    reload_manager.sync_pollers(conf, runtime)
    reload_manager.start_watch()

    def _shutdown(signum, frame):  # noqa: ARG001
        logger.info(f"收到信号 {signum},正在退出 ...")
        stop_event.set()
        reload_manager.stop_all()
        server.shutdown()
        engine.close()
        store.close()
        logger.info("已退出")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("ptpilot 启动完成")
    while not stop_event.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()
