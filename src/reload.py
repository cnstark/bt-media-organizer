"""配置热重载:文件变更监听(mtime 轮询)+ 手动触发,运行时应用新配置,无需重启。

设计要点:
- 轮询线程的 interval 存于 PollerState,热重载时原地调整,不重启线程;
- 下载器增删 / 凭据变化 → 重建适配器(httpx 客户端为无状态连接,重建安全);
- TMDB 识别器 / 辅种 matcher 持有初始化时快照(客户端、流控、tracker_map),变更时整体重建;
- 校验失败或应用异常 → 保留旧配置,记录 last_error 供查询;
- server.host/port 绑定于启动时,不支持热重载(需重启进程),其余均可生效。
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config, load_config
from .downloaders import QBittorrentAdapter, create_adapter
from .history import HistoryStore
from .recognize.tmdb import TmdbRecognizer
from .reseed.engine import ReseedEngine
from .reseed.matcher import JackettMatcher
from .reseed.store import ReseedStore
from .transfer.engine import TransferEngine as TransferEngineImpl

logger = logging.getLogger("ptpilot.reload")

# 轮询线程注册表键(与 runtime["pollers"] 对齐)
ORGANIZE = "organize"
TRANSFER = "transfer"
RESEED = "reseed"


class PollerState:
    """轮询线程运行时状态;interval 可在热重载时原地调整,线程无需重启。"""

    __slots__ = ("interval", "stop")

    def __init__(self, interval: float):
        self.interval = interval
        self.stop = threading.Event()


def organize_poll_loop(engine, downloader: str, state: PollerState) -> None:
    """整理引擎轮询线程:每 state.interval 秒对账一次指定下载器。"""
    logger.info(f"轮询线程已启动: {downloader} 每 {state.interval}s")
    while not state.stop.is_set():
        try:
            engine.poll_once(downloader=downloader)
        except Exception as e:  # noqa: BLE001
            logger.error(f"轮询异常[{downloader}]: {e}")
        state.stop.wait(state.interval)


def run_once_loop(engine, state: PollerState) -> None:
    """通用轮询循环(转移/辅种引擎共用):周期调 run_once。"""
    name = getattr(engine, "name", type(engine).__name__)
    logger.info(f"轮询线程已启动: {name} 每 {state.interval}s")
    while not state.stop.is_set():
        try:
            engine.run_once()
        except Exception as e:  # noqa: BLE001
            logger.error(f"轮询异常[{name}]: {e}")
        state.stop.wait(state.interval)


def _close(obj) -> None:
    """安全释放可关闭对象(适配器/识别器/matcher),容忍无 close 或缺属性。"""
    try:
        if obj is not None and hasattr(obj, "close"):
            obj.close()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"关闭资源失败: {e}")


def _diff(old: Config, new: Config) -> List[str]:
    """对比两版配置,返回发生变化的顶层节(section)名列表。"""
    old_d = dataclasses.asdict(old)
    new_d = dataclasses.asdict(new)
    return [name for name in old_d if old_d[name] != new_d[name]]


class ConfigManager:
    """配置热重载管理器:文件监听 + 手动 reload,负责把新配置应用到运行组件。

    runtime 字段(main.py 注入):
      conf             当前生效 Config
      store            HistoryStore(整理历史)
      engine           整理引擎(OrganizeEngine)
      adapters         {name: DownloaderAdapter}(转移/辅种共用)
      transfer_engine  TransferEngine 或 None
      reseed_engine    ReseedEngine 或 None
      reseed_store     ReseedStore 或 None
      server           ApiServer(更新 token / 挂载 reload_manager)
      pollers          {"organize": {name: PollerState},
                        "transfer": PollerState|None,
                        "reseed": PollerState|None}
    """

    def __init__(self, path: str, runtime: dict, watch_interval: float = 3.0):
        self.path = Path(path)
        self.runtime = runtime
        self.watch_interval = watch_interval
        self._lock = threading.Lock()
        self.last_reload: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_changed: List[str] = []
        self._watcher_stop = threading.Event()
        self._watcher: Optional[threading.Thread] = None

    # ---------------- 对外 ----------------

    def reload(self) -> dict:
        """重新加载并应用配置。校验/应用失败时保留旧配置,返回 code=1。"""
        with self._lock:
            try:
                new_conf = load_config(str(self.path))
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                logger.error(f"配置热重载失败(保留旧配置): {e}")
                return {"code": 1, "reloaded": False, "message": str(e), "changed": []}
            try:
                changed = self._apply(new_conf)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                logger.error(f"配置热重载应用失败(保留旧配置): {e}")
                return {"code": 1, "reloaded": False, "message": str(e), "changed": []}
            self.runtime["conf"] = new_conf
            self.last_reload = time.time()
            self.last_error = None
            self.last_changed = changed
            msg = "配置已热重载" + (f": 变更 {', '.join(changed)}" if changed else "(无变化)")
            logger.info(msg)
            return {"code": 0, "reloaded": True, "message": msg, "changed": changed}

    def status(self) -> dict:
        """热重载状态(供 GET /api/v1/config/status)。"""
        mtime = None
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            pass
        return {
            "path": str(self.path),
            "mtime": mtime,
            "watch": {"enabled": self.watch_interval > 0, "interval": self.watch_interval},
            "last_reload": self.last_reload,
            "last_error": self.last_error,
            "last_changed": self.last_changed,
        }

    def sync_pollers(self, conf: Config, runtime: dict) -> None:
        """按当前配置启动/停止/调整全部轮询线程(启动与热重载共用)。"""
        pollers = runtime["pollers"]
        # 整理引擎:每个下载器一个轮询线程
        org = pollers[ORGANIZE]
        wanted = {dl.name: dl.poll_interval for dl in conf.downloaders if dl.poll_interval > 0}
        for name in list(org):
            if name not in wanted:
                org[name].stop.set()
                del org[name]
        for name, interval in wanted.items():
            state = org.get(name)
            if state is None:
                state = PollerState(interval)
                org[name] = state
                t = threading.Thread(target=organize_poll_loop,
                                     args=(runtime["engine"], name, state),
                                     name=f"poll-{name}", daemon=True)
                t.start()
            else:
                state.interval = interval
        # 转移 / 辅种引擎轮询
        self._sync_engine_poller(TRANSFER, conf, runtime)
        self._sync_engine_poller(RESEED, conf, runtime)

    def start_watch(self) -> None:
        """启动配置文件监听线程(PTPILOT_WATCH_INTERVAL=0 时禁用)。"""
        if self.watch_interval <= 0:
            logger.info("配置热重载监听未启用(PTPILOT_WATCH_INTERVAL=0)")
            return
        self._watcher_stop.clear()
        self._watcher = threading.Thread(target=self._watch_loop,
                                         name="config-watch", daemon=True)
        self._watcher.start()

    def stop_all(self) -> None:
        """停止全部轮询线程与监听(进程退出用)。"""
        self._watcher_stop.set()
        pollers = self.runtime.get("pollers") or {}
        for state in pollers.get(ORGANIZE, {}).values():
            state.stop.set()
        for key in (TRANSFER, RESEED):
            state = pollers.get(key)
            if state:
                state.stop.set()

    # ---------------- 文件监听 ----------------

    def _watch_loop(self) -> None:
        last = None
        try:
            st = self.path.stat()
            last = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        logger.info(f"配置热重载监听已启动: {self.path} 每 {self.watch_interval}s")
        while not self._watcher_stop.is_set():
            self._watcher_stop.wait(self.watch_interval)
            if self._watcher_stop.is_set():
                return
            try:
                st = self.path.stat()
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
            if sig != last:
                last = sig
                logger.info("检测到配置文件变更,触发热重载 ...")
                self.reload()

    # ---------------- 应用新配置 ----------------

    def _apply(self, new_conf: Config) -> List[str]:
        rt = self.runtime
        old = rt.get("conf")
        changed = _diff(old, new_conf) if old is not None else ["all"]
        engine = rt["engine"]

        # 1. 整理引擎:运行时全部读 self.conf,直接换引用
        engine.conf = new_conf

        # 2. 下载器适配器(增删 / 凭据 / 路径变化)
        if "downloaders" in changed:
            self._rebuild_adapters(engine, new_conf, rt)

        # 3. TMDB 识别器(识别配置变化 → 重建)
        if "recognize" in changed:
            _close(engine.recognizer)
            engine.recognizer = TmdbRecognizer(new_conf.recognize.tmdb, rt["store"])

        # 4. 转移引擎
        rt[TRANSFER + "_engine"] = self._rebuild_transfer(new_conf, rt)

        # 5. 辅种引擎
        rt[RESEED + "_engine"] = self._rebuild_reseed(new_conf, rt)

        # 6. 历史存储(db 路径 / 保留天数变化 → 重建)
        if "history" in changed:
            store = HistoryStore(new_conf.history.db, new_conf.history.keep_days)
            store.purge()
            engine.store = store
            rt["store"] = store
            reseed_store = rt.get("reseed_store")
            if reseed_store:
                _close(reseed_store)
                rt["reseed_store"] = ReseedStore(new_conf.history.db)

        # 7. 日志级别(文件路径变化需重启生效)
        if "log" in changed:
            logger.setLevel(getattr(logging, new_conf.log.level.upper(), logging.INFO))

        # 8. 鉴权 token(后续请求立即生效)
        if "server" in changed:
            from .api.server import _Handler
            _Handler.token = new_conf.server.token

        # 9. 轮询线程(间隔 / 增减)
        self.sync_pollers(new_conf, rt)

        return changed

    # ---------------- 组件重建 ----------------

    def _rebuild_adapters(self, engine, conf: Config, rt: dict) -> None:
        """下载器配置变化 → 重建整理侧与主适配器表(旧客户端释放)。"""
        old_engine_adapters = list(engine.downloaders.values())
        engine.downloaders = {}
        for dl in conf.downloaders:
            if dl.type == "qbittorrent":
                try:
                    engine.downloaders[dl.name] = QBittorrentAdapter(dl)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"创建整理下载器适配器失败 [{dl.name}]: {e}")

        old_main = list(rt["adapters"].values())
        new_main = {}
        for dl in conf.downloaders:
            try:
                new_main[dl.name] = create_adapter(dl)
            except Exception as e:  # noqa: BLE001
                logger.error(f"创建下载器适配器失败 [{dl.name}]: {e}")
        rt["adapters"] = new_main

        for adapter in old_main + old_engine_adapters:
            _close(adapter)

    def _rebuild_transfer(self, conf: Config, rt: dict):
        """按新配置重建/更新转移引擎;禁用或适配器缺失时返回 None。"""
        cur = rt[TRANSFER + "_engine"]
        if not conf.transfer.enabled:
            return None
        from_adapter = rt["adapters"].get(conf.transfer.from_client)
        to_adapter = rt["adapters"].get(conf.transfer.to_client)
        if from_adapter is None or to_adapter is None:
            logger.error("transfer 模块启用但下载器适配器创建失败,转移功能不可用")
            return None
        if cur is None:
            cur = TransferEngineImpl(conf.transfer, from_adapter, to_adapter)
            logger.info(f"转移引擎已初始化: {conf.transfer.from_client} → {conf.transfer.to_client}")
        else:
            cur.conf = conf.transfer
            cur.from_adapter = from_adapter
            cur.to_adapter = to_adapter
        return cur

    def _rebuild_reseed(self, conf: Config, rt: dict):
        """按新配置重建/更新辅种引擎;matcher 持有快照(客户端/流控/tracker_map),每次重建。"""
        cur = rt[RESEED + "_engine"]
        if not conf.reseed.enabled:
            if cur is not None:
                _close(cur.matcher)  # 释放 Jackett 客户端
            return None
        target = rt["adapters"].get(conf.reseed.target_client)
        if target is None:
            logger.error("reseed 模块启用但目标下载器适配器创建失败,辅种功能不可用")
            return None
        reseed_store = rt.get("reseed_store")
        if reseed_store is None:
            reseed_store = ReseedStore(conf.history.db)
            rt["reseed_store"] = reseed_store
        if cur is None:
            cur = ReseedEngine(conf.reseed, rt["adapters"], reseed_store,
                               JackettMatcher(conf.reseed.jackett))
            logger.info(f"辅种引擎已初始化: 注入目标={conf.reseed.target_client}")
        else:
            cur.conf = conf.reseed
            cur.adapters = rt["adapters"]
            cur.target = target
            cur.store = reseed_store
            _close(cur.matcher)
            cur.matcher = JackettMatcher(conf.reseed.jackett)
        return cur

    def _sync_engine_poller(self, key: str, conf: Config, rt: dict) -> None:
        """转移/辅种单线程轮询:按启用状态与 poll_interval 启动/停止/调整。"""
        pollers = rt["pollers"]
        engine = rt[f"{key}_engine"]
        interval = 0
        if engine is not None:
            interval = conf.transfer.poll_interval if key == TRANSFER else conf.reseed.poll_interval
        state = pollers[key]
        if engine is not None and interval > 0:
            if state is None:
                state = PollerState(interval)
                pollers[key] = state
                t = threading.Thread(target=run_once_loop, args=(engine, state),
                                     name=f"{key}-poll", daemon=True)
                t.start()
            else:
                state.interval = interval
        elif state is not None:
            state.stop.set()
            pollers[key] = None
