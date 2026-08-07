"""organize 顶层层级(整理相关配置收纳)加载测试。

覆盖:organize 层级收纳 engine/directories/recognize、顶层平铺旧写法被忽略/报错、transfer/reseed 不受影响。
运行: .venv/bin/python -m pytest tests/test_config_organize_level.py -q
"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402


def _write(tmp: Path, body: str) -> Path:
    cfg = tmp / "config.yaml"
    cfg.write_text(f"""
server:
  host: "127.0.0.1"
  port: 18900
  token: "test-token"
{body}
history:
  db: "{tmp / 'data' / 'organizer.db'}"
log:
  level: warning
""", encoding="utf-8")
    return cfg


def test_organize_level_loads_engine_dirs_recognize():
    """新层级 organize 收纳 engine/directories/recognize,加载结果与旧布局一致。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg = _write(tmp, """
organize:
  engine:
    threads: 4
    default_overwrite: latest
    rename:
      movie: "{{title}}/{{title}}{{ext}}"
      tv: "{{title}}/{{season_dir}}/{{title}} - {{season_episode}}{{ext}}"
  directories:
    - name: "电影"
      download_path: "/d/movies"
      library_path: "/l/movies"
      transfer_type: hardlink
      media_type: movie
  recognize:
    tmdb:
      enabled: true
      api_key: "abc123"
      language: "zh-CN"
downloaders: []
""")
        conf = load_config(str(cfg))
        assert conf.engine.threads == 4
        assert conf.engine.default_overwrite == "latest"
        assert conf.engine.rename.movie == "{{title}}/{{title}}{{ext}}"
        assert [d.name for d in conf.directories] == ["电影"]
        assert conf.directories[0].transfer_type == "hardlink"
        assert conf.recognize.tmdb.enabled is True
        assert conf.recognize.tmdb.api_key == "abc123"


def test_flat_keys_ignored():
    """仅从 organize 层级读取;顶层平铺的 engine/directories 一律忽略。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg = _write(tmp, """
organize:
  engine:
    threads: 8
  directories:
    - name: "新目录"
      download_path: "/d/new"
      library_path: "/l/new"
      transfer_type: hardlink
      media_type: movie
engine:
  threads: 1
directories:
  - name: "旧目录"
    download_path: "/d/old"
    library_path: "/l/old"
    transfer_type: copy
    media_type: tv
downloaders: []
""")
        conf = load_config(str(cfg))
        assert conf.engine.threads == 8
        assert [d.name for d in conf.directories] == ["新目录"]


def test_missing_organize_level_raises():
    """不写 organize 层级、直接平铺旧写法 → 加载报错(不再兼容)。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg = _write(tmp, """
engine:
  threads: 2
directories: []
downloaders: []
""")
        with pytest.raises(ValueError):
            load_config(str(cfg))


def test_transfer_reseed_unaffected_by_organize_level():
    """organize 层级不影响 downloaders/transfer/reseed 顶层配置加载。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg = _write(tmp, """
organize:
  engine:
    threads: 2
  directories: []
  recognize:
    tmdb:
      enabled: false
downloaders:
  - name: qb
    type: qbittorrent
    url: "http://127.0.0.1:8080"
    poll_interval: 60
    tag: "已整理"
  - name: tr
    type: transmission
    url: "http://127.0.0.1:9091"
    poll_interval: 0
    tag: "已整理"
transfer:
  enabled: true
  from_client: qb
  to_client: tr
  marker: tag
reseed:
  enabled: true
  target_client: tr
  matcher: jackett
  jackett:
    url: "http://127.0.0.1:9117"
    api_key: "k"
    indexers: ["btschool"]
""")
        conf = load_config(str(cfg))
        assert [d.name for d in conf.downloaders] == ["qb", "tr"]
        assert conf.transfer.enabled is True
        assert conf.transfer.from_client == "qb"
        assert conf.reseed.enabled is True
        assert conf.reseed.target_client == "tr"
        assert conf.reseed.jackett.indexers == ["btschool"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
