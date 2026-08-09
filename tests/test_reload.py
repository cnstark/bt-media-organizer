"""配置热重载(ConfigManager)单测。

覆盖:热重载应用新配置 / 校验失败保留旧配置 / 轮询线程增减与间隔调整 /
文件监听自动触发 / 转移与辅种引擎按新配置重建。
运行: .venv/bin/python -m pytest tests/test_reload.py -q
"""
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.server import _Handler  # noqa: E402
from src.config import load_config  # noqa: E402
from src.reload import ConfigManager, PollerState  # noqa: E402


class FakeEngine:
    """整理引擎桩:记录 conf 交换,支持 poll_once / downloaders。"""

    def __init__(self, conf):
        self.conf = conf
        self.store = None
        self.recognizer = SimpleNamespace(closed=False, close=lambda: setattr(self.recognizer, "closed", True))
        self.downloaders = {}

    def poll_once(self, downloader=None):
        return {}


class FakeAdapter:
    def __init__(self, name):
        self.name = name

    def list_torrents(self, state="all"):
        return []

    def close(self):
        pass


def _write_config(tmp: Path, body_extra: str = "", transfer: str = "",
                  reseed: str = "") -> Path:
    cfg = tmp / "config.yaml"
    cfg.write_text(f"""
server:
  host: "127.0.0.1"
  port: 18900
  token: "test-token"
organize:
  engine:
    threads: 2
  directories:
    - name: "电影"
      download_path: "/d/movies"
      library_path: "/l/movies"
      transfer_type: hardlink
      media_type: movie
downloaders:
  - name: "qb"
    type: qbittorrent
    url: "http://127.0.0.1:8080"
{body_extra}
history:
  db: "{tmp / 'data' / 'organizer.db'}"
log:
  level: warning
{transfer}
{reseed}
""", encoding="utf-8")
    return cfg


def _make_runtime(conf, engine, adapters=None):
    return {
        "conf": conf,
        "store": SimpleNamespace(),
        "engine": engine,
        "adapters": adapters if adapters is not None else {},
        "transfer_engine": None,
        "reseed_engine": None,
        "reseed_store": None,
        "server": None,
        "pollers": {"organize": {}, "transfer": None, "reseed": None},
    }


@pytest.fixture(autouse=True)
def _save_token():
    old = _Handler.token
    _Handler.token = ""
    yield
    _Handler.token = old


def test_reload_applies_new_config_and_token():
    """修改 engine/log/server 后 reload:conf 交换、token 更新、变更节上报。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg_path = _write_config(tmp)
        conf = load_config(str(cfg_path))
        engine = FakeEngine(conf)
        runtime = _make_runtime(conf, engine)
        cm = ConfigManager(str(cfg_path), runtime, watch_interval=0)

        cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace(
            "threads: 2", "threads: 4").replace(
            'token: "test-token"', 'token: "new-token"').replace(
            "level: warning", "level: debug"), encoding="utf-8")

        result = cm.reload()
        assert result["reloaded"] is True
        assert result["code"] == 0
        assert {"engine", "server", "log"} <= set(result["changed"])
        assert engine.conf.engine.threads == 4
        assert _Handler.token == "new-token"
        assert cm.last_error is None
        assert cm.last_changed == result["changed"]


def test_reload_validation_failure_keeps_old():
    """新配置校验失败(缺 organize)→ 保留旧配置并记录错误。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg_path = _write_config(tmp)
        conf = load_config(str(cfg_path))
        engine = FakeEngine(conf)
        runtime = _make_runtime(conf, engine)
        cm = ConfigManager(str(cfg_path), runtime, watch_interval=0)

        cfg_path.write_text("server:\n  token: x\n", encoding="utf-8")
        result = cm.reload()
        assert result["reloaded"] is False
        assert result["code"] == 1
        assert "organize" in result["message"]
        assert engine.conf.engine.threads == 2  # 旧配置未动
        assert cm.last_error == result["message"]
        assert cm.last_reload is None


def test_reload_yaml_error_keeps_old():
    """新配置 YAML 语法错误 → 保留旧配置。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg_path = _write_config(tmp)
        conf = load_config(str(cfg_path))
        engine = FakeEngine(conf)
        runtime = _make_runtime(conf, engine)
        cm = ConfigManager(str(cfg_path), runtime, watch_interval=0)

        cfg_path.write_text("server: [unclosed\n", encoding="utf-8")
        result = cm.reload()
        assert result["reloaded"] is False
        assert result["code"] == 1
        assert engine.conf.engine.threads == 2


def test_sync_pollers_add_remove_and_interval():
    """轮询线程:按下载器增减,interval 原地调整。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg_path = _write_config(tmp, body_extra="""
  - name: "tr"
    type: transmission
    url: "http://127.0.0.1:9091"
    poll_interval: 10
""")
        # qb 显式 poll_interval 5(写回文件,热重载按文件为准)
        text = cfg_path.read_text(encoding="utf-8")
        cfg_path.write_text(text.replace(
            'url: "http://127.0.0.1:8080"',
            'url: "http://127.0.0.1:8080"\n    poll_interval: 5'),
            encoding="utf-8")
        conf = load_config(str(cfg_path))
        engine = FakeEngine(conf)
        runtime = _make_runtime(conf, engine)
        cm = ConfigManager(str(cfg_path), runtime, watch_interval=0)
        cm.sync_pollers(conf, runtime)

        pollers = runtime["pollers"]["organize"]
        assert set(pollers) == {"qb", "tr"}
        assert pollers["qb"].interval == 5
        assert pollers["tr"].interval == 10

        # 移除 tr,qb 间隔改 7
        text = cfg_path.read_text(encoding="utf-8")
        text = text.replace('  - name: "tr"\n    type: transmission\n    url: "http://127.0.0.1:9091"\n    poll_interval: 10\n', "")
        text = text.replace("poll_interval: 5", "poll_interval: 7")
        cfg_path.write_text(text, encoding="utf-8")
        cm.reload()

        pollers = runtime["pollers"]["organize"]
        assert set(pollers) == {"qb"}
        assert pollers["qb"].interval == 7
        cm.stop_all()


def test_watch_loop_triggers_reload_on_change():
    """文件变更(mtime+size)→ 监听线程自动 reload。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg_path = _write_config(tmp)
        conf = load_config(str(cfg_path))
        engine = FakeEngine(conf)
        runtime = _make_runtime(conf, engine)
        cm = ConfigManager(str(cfg_path), runtime, watch_interval=0.1)
        cm.start_watch()
        try:
            # 启动后文件未变:不应误触发重载(回归:签名类型不一致 bug)
            time.sleep(0.35)
            assert cm.last_reload is None
            cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace(
                "threads: 2", "threads: 6"), encoding="utf-8")
            deadline = time.time() + 3
            while cm.last_reload is None and time.time() < deadline:
                time.sleep(0.05)
            assert cm.last_reload is not None
            assert engine.conf.engine.threads == 6
        finally:
            cm.stop_all()


def test_transfer_reseed_engine_rebuild():
    """转移/辅种模块:启用→重建引擎与 matcher,禁用→停引擎与轮询。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 需含第二个下载器 tr(转移/辅种的目标),校验要求引用存在
        cfg_path = _write_config(tmp, body_extra="""
  - name: "tr"
    type: transmission
    url: "http://127.0.0.1:9091"
""")
        conf = load_config(str(cfg_path))
        engine = FakeEngine(conf)
        adapters = {"qb": FakeAdapter("qb"), "tr": FakeAdapter("tr")}
        runtime = _make_runtime(conf, engine, adapters)
        cm = ConfigManager(str(cfg_path), runtime, watch_interval=0)

        # 启用转移
        cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + """
transfer:
  enabled: true
  from_client: "qb"
  to_client: "tr"
  poll_interval: 300
""", encoding="utf-8")
        result = cm.reload()
        assert result["reloaded"] is True
        te = runtime["transfer_engine"]
        assert te is not None
        assert te.from_adapter.name == "qb" and te.to_adapter.name == "tr"
        assert runtime["pollers"]["transfer"] is not None
        assert runtime["pollers"]["transfer"].interval == 300

        # 启用辅种(白名单必填)
        cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + """
reseed:
  enabled: true
  target_client: "tr"
  poll_interval: 3600
  matcher: jackett
  jackett:
    url: "http://127.0.0.1:9117"
    api_key: "test-key"
    indexers: ["btschool"]
""", encoding="utf-8")
        result = cm.reload()
        assert result["reloaded"] is True
        re_ = runtime["reseed_engine"]
        assert re_ is not None
        assert re_.target.name == "tr"
        old_matcher = re_.matcher
        assert runtime["pollers"]["reseed"] is not None
        assert runtime["pollers"]["reseed"].interval == 3600

        # 白名单变更 → matcher 重建(新 conf 生效)
        cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace(
            'indexers: ["btschool"]', 'indexers: ["btschool", "haidan"]'), encoding="utf-8")
        result = cm.reload()
        assert result["reloaded"] is True
        assert runtime["reseed_engine"] is re_  # 引擎对象复用
        assert runtime["reseed_engine"].matcher is not old_matcher  # matcher 重建
        assert runtime["reseed_engine"].conf.jackett.indexers == ["btschool", "haidan"]

        # 全部禁用 → 引擎置空、轮询停止
        text = cfg_path.read_text(encoding="utf-8")
        text = text.replace("enabled: true", "enabled: false")
        cfg_path.write_text(text, encoding="utf-8")
        result = cm.reload()
        assert result["reloaded"] is True
        assert runtime["transfer_engine"] is None
        assert runtime["reseed_engine"] is None
        assert runtime["pollers"]["transfer"] is None
        assert runtime["pollers"]["reseed"] is None
        cm.stop_all()


def test_status_shape():
    """status() 输出结构。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg_path = _write_config(tmp)
        conf = load_config(str(cfg_path))
        runtime = _make_runtime(conf, FakeEngine(conf))
        cm = ConfigManager(str(cfg_path), runtime, watch_interval=2.5)
        st = cm.status()
        assert st["path"] == str(cfg_path)
        assert st["watch"] == {"enabled": True, "interval": 2.5}
        assert st["mtime"] is not None
        assert st["last_reload"] is None
        assert st["last_error"] is None
        assert st["last_changed"] == []
