"""命名模板渲染测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.engine.namer import render_path, sanitize  # noqa: E402
from src.parse.filename import parse_filename  # noqa: E402
from src.recognize.tmdb import MediaInfo  # noqa: E402

MOVIE_TPL = ("{{title}}{% if year %} ({{year}}){% endif %}"
             "/{{title}}{% if year %} ({{year}}){% endif %}"
             "{% if part %}-{{part}}{% endif %}"
             "{% if quality %} - {{quality}}{% endif %}{{ext}}")
TV_TPL = ("{{title}}{% if year %} ({{year}}){% endif %}"
          "/{{season_dir}}/{{title}} - {{season_episode}}"
          "{% if part %}-{{part}}{% endif %}{{ext}}")
S0 = ["Specials", "SPs"]


def test_movie_render():
    meta = parse_filename("Movie.2026.1080p.BluRay.x264-GROUP.mkv")
    path = render_path(meta, None, MOVIE_TPL, S0)
    assert path == "Movie (2026)/Movie (2026) - 1080p.BluRay.x264.mkv", path


def test_movie_no_year():
    meta = parse_filename("SomeMovie.1080p.mkv")
    path = render_path(meta, None, MOVIE_TPL, S0)
    assert path == "SomeMovie/SomeMovie - 1080p.mkv", path


def test_tv_render():
    meta = parse_filename("Show.2021.S01E02.1080p.x265.mkv")
    path = render_path(meta, None, TV_TPL, S0)
    assert path == "Show (2021)/Season 1/Show - S01E02.mkv", path


def test_tv_s0_alias():
    meta = parse_filename("Show.S00E01.1080p.mkv")
    path = render_path(meta, None, TV_TPL, S0)
    assert path == "Show/Specials/Show - S00E01.mkv", path


def test_tmdb_title_override():
    meta = parse_filename("maze.runner.2014.1080p.mkv")
    media = MediaInfo(title="移动迷宫", year=2014, media_type="movie", tmdb_id=198663)
    path = render_path(meta, media, MOVIE_TPL, S0)
    assert path == "移动迷宫 (2014)/移动迷宫 (2014) - 1080p.mkv", path


def test_sanitize():
    assert sanitize('A/B:C*D?"E<F>G|H') == "A B C D E F G H"


def test_part_render():
    meta = parse_filename("Movie.2020.1080p.x264-PART1.mkv")
    path = render_path(meta, None, MOVIE_TPL, S0)
    assert path == "Movie (2020)/Movie (2020)-Part1 - 1080p.x264.mkv", path


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
