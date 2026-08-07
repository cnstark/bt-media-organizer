"""端到端集成测试:真实文件整理、幂等、redo(不依赖 qBittorrent 网络)。

运行: .venv/bin/python tests/test_integration.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.engine import TransferEngine  # noqa: E402
from src.history import HistoryStore  # noqa: E402


def _write_config(tmp: Path, transfer_type: str = "copy") -> Path:
    cfg = tmp / "config.yaml"
    cfg.write_text(f"""
server:
  host: "127.0.0.1"
  port: 18900
  token: "test-token"
organize:
  engine:
    threads: 2
    rename:
      movie: "{{{{title}}}}{{% if year %}} ({{{{year}}}}){{% endif %}}/{{{{title}}}}{{% if year %}} ({{{{year}}}}){{% endif %}}{{% if quality %}} - {{{{quality}}}}{{% endif %}}{{{{ext}}}}"
      tv: "{{{{title}}}}{{% if year %}} ({{{{year}}}}){{% endif %}}/{{{{season_dir}}}}/{{{{title}}}} - {{{{season_episode}}}}{{{{ext}}}}"
    default_overwrite: never
  directories:
    - name: "movies"
      download_path: "{tmp / 'downloads' / 'movies'}"
      library_path: "{tmp / 'media'}"
      transfer_type: {transfer_type}
      media_type: movie
downloaders: []
history:
  db: "{tmp / 'data' / 'organizer.db'}"
log:
  level: warning
""", encoding="utf-8")
    return cfg


def test_organize_movie():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        downloads = tmp / "downloads" / "movies"
        downloads.mkdir(parents=True)
        movie = downloads / "Dune.2021.1080p.BluRay.x264-GROUP.mkv"
        movie.write_bytes(b"x" * 1024)
        sub = downloads / "Dune.2021.chs.ass"
        sub.write_bytes(b"y" * 128)

        conf = load_config(str(_write_config(tmp)))
        store = HistoryStore(conf.history.db)
        engine = TransferEngine(conf, store)
        try:
            # 预览
            result = engine.organize(movie, preview=True)
            assert result.total == 2, result.items
            assert result.preview and result.all_success
            target = result.items[0]["target"]
            assert target.endswith("Dune (2021)/Dune (2021) - 1080p.BluRay.x264.mkv"), target

            # 真实整理
            result = engine.organize(movie)
            assert result.all_success and result.success == 2, result
            organized = Path(result.items[0]["target"])
            assert organized.exists()
            sub_target = Path(result.items[1]["target"])
            assert sub_target.exists()
            # 字幕语言标记
            assert sub_target.name == "Dune (2021) - 1080p.BluRay.x264.zh-cn.ass", sub_target.name

            # 幂等:再次整理应全部跳过
            result2 = engine.organize(movie)
            assert result2.skipped == 2 and result2.success == 0, result2

            # 历史记录
            rows = store.list(limit=10)
            assert len(rows) == 2
            assert all(r.status == "success" for r in rows)

            # redo
            ok, msg, _ = engine.redo(rows[0].id)
            assert ok, msg
            result3 = engine.organize(movie)
            assert result3.skipped == 2

            # 源文件保留(copy 模式)
            assert movie.exists()
        finally:
            engine.close()
            store.close()


def test_organize_tv_move():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        downloads = tmp / "downloads" / "movies"   # 与配置 download_path 一致
        downloads.mkdir(parents=True)
        ep = downloads / "觉醒年代.2021.S01E01.1080p.x265.mkv"
        ep.write_bytes(b"z" * 1024)

        conf = load_config(str(_write_config(tmp, transfer_type="move")))
        conf.directories[0].media_type = "tv"
        store = HistoryStore(conf.history.db)
        engine = TransferEngine(conf, store)
        try:
            result = engine.organize(ep)
            assert result.all_success, result
            target = Path(result.items[0]["target"])
            assert target.exists()
            assert target.name == "觉醒年代 - S01E01.mkv", target.name
            assert target.parent.name == "Season 1", target.parent.name
            # move 模式:源文件已移走;下载根目录作为清理边界保留
            assert not ep.exists()
            assert downloads.exists()
            assert not list(downloads.iterdir()), "下载目录应为空"
        finally:
            engine.close()
            store.close()


def test_overwrite_never():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        downloads = tmp / "downloads" / "movies"
        downloads.mkdir(parents=True)
        media = tmp / "media"
        movie = downloads / "Movie.2020.1080p.mkv"
        movie.write_bytes(b"a" * 1024)

        conf = load_config(str(_write_config(tmp)))
        store = HistoryStore(conf.history.db)
        engine = TransferEngine(conf, store)
        try:
            r1 = engine.organize(movie)
            assert r1.all_success
            # 目标已存在,never 模式应失败且不影响已有文件
            movie.write_bytes(b"b" * 2048)
            r2 = engine.organize(movie, force=True)
            assert r2.failed == 1 and not r2.all_success, r2
            assert r2.items[0]["message"].startswith("目标已存在")
        finally:
            engine.close()
            store.close()


def test_delete_history_only_keeps_files():
    """仅删除历史记录:文件不受影响,幂等失效。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        downloads = tmp / "downloads" / "movies"
        downloads.mkdir(parents=True)
        movie = downloads / "Dune.2021.1080p.BluRay.x264-GROUP.mkv"
        movie.write_bytes(b"x" * 1024)

        conf = load_config(str(_write_config(tmp)))
        store = HistoryStore(conf.history.db)
        engine = TransferEngine(conf, store)
        try:
            result = engine.organize(movie)
            assert result.all_success and result.success == 1, result
            row = store.list(limit=10)[0]
            target = Path(row.target_path)
            assert target.exists()

            ok, msg = engine.delete_history(row.id)
            assert ok, msg
            assert store.get_by_id(row.id) is None
            assert target.exists(), "仅删历史不应动文件"
            assert movie.exists()

            # 幂等失效:不再跳过,但因目标已存在(never 策略)整理失败
            result2 = engine.organize(movie)
            assert result2.failed == 1 and "目标已存在" in result2.items[0]["message"], result2
        finally:
            engine.close()
            store.close()


def test_delete_history_files():
    """删除整理产物:目标/源/历史/空目录清理的完整组合。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        downloads = tmp / "downloads" / "movies"
        downloads.mkdir(parents=True)
        movie = downloads / "Dune.2021.1080p.BluRay.x264-GROUP.mkv"
        movie.write_bytes(b"x" * 1024)
        sub = downloads / "Dune.2021.chs.ass"
        sub.write_bytes(b"y" * 128)

        conf = load_config(str(_write_config(tmp)))
        store = HistoryStore(conf.history.db)
        engine = TransferEngine(conf, store)
        try:
            result = engine.organize(movie)
            assert result.all_success and result.success == 2, result
            target = Path(result.items[0]["target"])
            sub_target = Path(result.items[1]["target"])
            rows = store.list(limit=10)
            assert len(rows) == 2
            movie_row = next(r for r in rows if r.target_path == str(target))
            sub_row = next(r for r in rows if r.target_path == str(sub_target))

            # 1) 删文件(不删历史):目标删除,源保留,历史保留;
            #    目标目录里还有字幕文件 → 不清理目录
            ok, msg, data = engine.delete_history_files(movie_row.id)
            assert ok, msg
            assert not target.exists()
            assert movie.exists()
            assert store.get_by_id(movie_row.id) is not None
            assert data["deleted_files"] == [str(target)]
            assert target.parent.exists()

            # 2) 目标已不存在 → 记入 missing,不报错
            ok, msg, data = engine.delete_history_files(movie_row.id)
            assert ok, msg
            assert data["missing"] == [str(target)]

            # 3) 删字幕文件 + 连源文件 + 连历史:目录变空被清理,库根保留
            ok, msg, data = engine.delete_history_files(
                sub_row.id, delete_source=True, delete_history=True)
            assert ok, msg
            assert not sub_target.exists()
            assert not sub.exists()
            assert store.get_by_id(sub_row.id) is None
            assert data["deleted_source"] == [str(sub)]
            assert not sub_target.parent.exists(), "空的目标目录应被清理"
            assert (tmp / "media").exists(), "库根目录应保留"

            # 4) 记录不存在
            ok, msg, _ = engine.delete_history_files(99999)
            assert not ok and "不存在" in msg
        finally:
            engine.close()
            store.close()


def test_delete_files_grouped_history():
    """delete_history=True 时,同 hash 的历史记录一并删除。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        downloads = tmp / "downloads" / "movies"
        downloads.mkdir(parents=True)
        movie = downloads / "Dune.2021.1080p.BluRay.x264-GROUP.mkv"
        movie.write_bytes(b"x" * 1024)
        sub = downloads / "Dune.2021.chs.ass"
        sub.write_bytes(b"y" * 128)

        conf = load_config(str(_write_config(tmp)))
        store = HistoryStore(conf.history.db)
        engine = TransferEngine(conf, store)
        try:
            result = engine.organize(movie, download_hash="deadbeef")
            assert result.all_success and result.success == 2, result
            rows = store.list(limit=10)
            assert len(rows) == 2 and all(r.download_hash == "deadbeef" for r in rows)

            ok, msg, data = engine.delete_history_files(rows[0].id, delete_history=True)
            assert ok, msg
            assert data["deleted_history"] == [rows[0].id, rows[1].id]
            assert store.get_by_id(rows[0].id) is None
            assert store.get_by_id(rows[1].id) is None
        finally:
            engine.close()
            store.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
