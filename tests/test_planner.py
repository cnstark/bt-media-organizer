"""规划器测试:收集/过滤/排序/蓝光/附加文件归属。"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import EngineConf  # noqa: E402
from src.engine.planner import is_bluray_dir, plan  # noqa: E402


def _mkconf() -> EngineConf:
    return EngineConf()


def test_single_file_with_extras():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "Movie.2026.1080p.mkv").write_bytes(b"x")
        (d / "Movie.2026.chs.ass").write_bytes(b"x")
        (d / "Movie.2026.eng.srt").write_bytes(b"x")
        (d / "movie.part").write_bytes(b"x")  # 临时文件应被过滤

        items = plan(d / "Movie.2026.1080p.mkv", _mkconf(), [])
        kinds = [it.kind for it in items]
        assert kinds == ["main", "subtitle", "subtitle"], kinds
        # 附加文件归属主视频
        assert all(it.related is not None and it.related.kind == "main" for it in items[1:])
        # 临时文件未进入规划
        assert all(it.source.name != "movie.part" for it in items)


def test_dir_recursive_and_filter():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Show.S01").mkdir()
        (root / "Show.S01" / "Show.S01E01.mkv").write_bytes(b"x" * (2 * 1024 * 1024))  # 2MB,满足 min_filesize
        (root / "Show.S01" / "Show.S01E01.chs.srt").write_bytes(b"x")
        (root / "Show.S01" / ".hidden.mkv").write_bytes(b"x")  # 隐藏文件过滤
        (root / "README.txt").write_bytes(b"x")                # 非媒体扩展名过滤
        (root / "Show.S01" / "small.mkv").write_bytes(b"x")    # 小于 min_filesize

        conf = _mkconf()
        conf.min_filesize = 1  # 1MB
        items = plan(root, conf, [])
        names = [it.source.name for it in items]
        assert "small.mkv" not in names, names
        assert "README.txt" not in names
        assert ".hidden.mkv" not in names
        assert items[0].kind == "main"
        # 附加文件跟随主视频
        assert items[1].kind == "subtitle" and items[1].related is items[0]


def test_bluray_dir():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Movie.2026.BluRay"
        (root / "BDMV" / "STREAM").mkdir(parents=True)
        (root / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"x")
        (root / "BDMV" / "index.bdmv").write_bytes(b"x")

        assert is_bluray_dir(root)
        items = plan(root, _mkconf(), [])
        assert len(items) == 1
        assert items[0].kind == "bluray"
        assert items[0].meta.title == "Movie"

        # 内部文件路径应整体提升为原盘
        items2 = plan(root / "BDMV" / "STREAM" / "00000.m2ts", _mkconf(), [])
        assert len(items2) == 1 and items2[0].kind == "bluray", items2


def test_exclude_words():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "Movie.2026.1080p.mkv").write_bytes(b"x")
        (d / "Blocked.Show.S01E01.mkv").write_bytes(b"x")
        items = plan(d, _mkconf(), ["Blocked"])
        assert [it.source.name for it in items] == ["Movie.2026.1080p.mkv"]


def test_tmp_ext_filtered():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "Movie.2026.mkv.!qb").write_bytes(b"x")
        (d / "Movie.2026.mkv").write_bytes(b"x")
        items = plan(d, _mkconf(), [])
        assert [it.source.name for it in items] == ["Movie.2026.mkv"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
