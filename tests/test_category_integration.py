"""类别目录集成测试:覆盖 _category_root 真实调用路径(回归 parse_rules 崩溃)。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.engine import TransferEngine  # noqa: E402
from src.history import HistoryStore  # noqa: E402
from src.recognize.tmdb import MediaInfo  # noqa: E402


def _make_engine(tmp: Path, extra: str = "") -> TransferEngine:
    cfg = tmp / "config.yaml"
    cfg.write_text(f"""
server:
  host: "127.0.0.1"
  port: 18900
  token: "t"
engine:
  threads: 1
directories:
  - name: "tv"
    download_path: "{tmp / 'dl'}"
    library_path: "{tmp / 'lib'}"
    transfer_type: copy
    media_type: tv
    category_folder: true
{extra}
downloaders: []
history:
  db: "{tmp / 'data.db'}"
log:
  level: warning
""", encoding="utf-8")
    conf = load_config(str(cfg))
    store = HistoryStore(conf.history.db)
    return TransferEngine(conf, store)


def test_default_rules_documentary():
    """空 category_rules + 识别成功 → 走内置默认规则,不崩溃,纪录片命中。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine = _make_engine(Path(tmp))
        try:
            media = MediaInfo(media_type="tv", genre_ids=[99], origin_country=["CN"], title="舌尖上的中国")
            root = engine._category_root(Path("/lib"), engine.conf.directories[0], media)
            assert root == Path("/lib/纪录片"), root
        finally:
            engine.close()


def test_default_rules_oumei():
    with tempfile.TemporaryDirectory() as tmp:
        engine = _make_engine(Path(tmp))
        try:
            media = MediaInfo(media_type="tv", genre_ids=[18], origin_country=["US"])
            root = engine._category_root(Path("/lib"), engine.conf.directories[0], media)
            assert root == Path("/lib/欧美剧"), root
        finally:
            engine.close()


def test_custom_rules_dict():
    """用户配置 category_rules(dict) → 解析生效。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine = _make_engine(Path(tmp), extra="""    category_rules:
      纪录片: { genre_ids: "99" }
      其他: {}""")
        try:
            media = MediaInfo(media_type="tv", genre_ids=[99], origin_country=["CN"])
            root = engine._category_root(Path("/lib"), engine.conf.directories[0], media)
            assert root == Path("/lib/纪录片"), root
            media2 = MediaInfo(media_type="tv", genre_ids=[18], origin_country=["CN"])
            root2 = engine._category_root(Path("/lib"), engine.conf.directories[0], media2)
            assert root2 == Path("/lib/其他"), root2
        finally:
            engine.close()


def test_no_media_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        engine = _make_engine(Path(tmp))
        try:
            root = engine._category_root(Path("/lib"), engine.conf.directories[0], None)
            assert root == Path("/lib/未分类"), root
        finally:
            engine.close()


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
