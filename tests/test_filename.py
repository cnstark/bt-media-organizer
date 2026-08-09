"""文件名解析测试(可直接 python 运行,无 pytest 依赖)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.parse.filename import (  # noqa: E402
    normalize_stem,
    parse_filename,
    strip_lang_tag,
    subtitle_lang_tag,
)


def test_movie():
    m = parse_filename("Movie.2026.1080p.BluRay.x264-GROUP.mkv")
    assert m.title == "Movie", m.title
    assert m.year == 2026, m.year
    assert m.resolution == "1080p"
    assert m.source == "BluRay"
    assert m.video_codec == "x264"
    assert m.group == "GROUP"
    assert m.ext == ".mkv"
    assert not m.is_tv
    assert m.quality == "1080p.BluRay.x264"


def test_movie_remux():
    m = parse_filename("Dune.2021.2160p.UHD.BluRay.REMUX.HEVC.DV.TrueHD.7.1.Atmos-FRAGMENT.mkv")
    assert m.title == "Dune"
    assert m.year == 2021
    assert m.resolution == "2160p"
    assert m.source == "Remux"
    assert m.video_codec == "hevc"
    assert m.audio_codec == "truehd"
    assert m.group == "FRAGMENT"


def test_tv_multi_episode():
    m = parse_filename("觉醒年代.2021.S01E01-E02.1080p.x265.mkv")
    assert m.title == "觉醒年代"
    assert m.year == 2021
    assert m.season == 1
    assert m.begin_episode == 1
    assert m.end_episode == 2
    assert m.is_tv
    assert m.season_episode == "S01E01"


def test_tv_season_only():
    m = parse_filename("Game.of.Thrones.S01.1080p.mkv")
    assert m.title == "Game of Thrones"
    assert m.season == 1
    assert m.begin_episode is None


def test_tv_season_word():
    m = parse_filename("Breaking.Bad.Season.2.EP03.720p.WEB-DL.mkv")
    assert m.title == "Breaking Bad"
    assert m.season == 2
    assert m.begin_episode == 3


def test_tv_chinese_episode():
    m = parse_filename("狂飙.第1季.第2集.1080p.mkv")
    assert m.title == "狂飙"
    assert m.season == 1
    assert m.begin_episode == 2


def test_brackets():
    m = parse_filename("[SubGroup] Title.2022.[1080p][x265].mp4")
    assert m.year == 2022
    assert m.resolution == "1080p"
    assert m.video_codec == "x265"
    assert m.title, "标题不应为空"


def test_part():
    m = parse_filename("Movie.2020.1080p.BluRay.x264-PART1.mkv")
    assert m.part == "Part1", m.part


def test_year_in_title_not_misparsed():
    m = parse_filename("2001.A.Space.Odyssey.1968.1080p.BluRay.x264.mkv")
    assert m.year == 1968, m.year
    assert m.title == "2001 A Space Odyssey", m.title


def test_subtitle_lang():
    assert subtitle_lang_tag("Movie.chs.srt") == ".zh-cn"
    assert subtitle_lang_tag("Movie.简中.ass") == ".zh-cn"
    assert subtitle_lang_tag("Movie.cht.srt") == ".zh-tw"
    assert subtitle_lang_tag("Movie.繁中.ass") == ".zh-tw"
    assert subtitle_lang_tag("Movie.ja.srt") == ".ja"
    assert subtitle_lang_tag("Movie.eng.srt") == ".eng"
    assert subtitle_lang_tag("Movie.2026.mkv") == ""


def test_normalize_and_strip():
    assert normalize_stem("Movie.2026.1080p") == "movie20261080p"
    # 语言标记连同分隔点一起被去除
    assert strip_lang_tag("Movie.chs.ass") == "Movieass"
    assert normalize_stem(strip_lang_tag("Movie.chs.ass")) == "movieass"
    assert normalize_stem("Movie.2026.mkv") == "movie2026mkv"


def test_orphan_extra_meta():
    m = parse_filename("Movie.2026.chs.ass")
    assert m.title == "Movie"
    assert m.year == 2026



def test_cn_numeral_season_episode():
    m = parse_filename("某剧.第四季.第5集.1080p.mkv")
    assert m.season == 4, m.season
    assert m.begin_episode == 5
    m2 = parse_filename("某剧.第十二季.第二十一集.1080p.mkv")
    assert m2.season == 12
    assert m2.begin_episode == 21


def test_at_group_imax_region_cleaned():
    # 蓝光原盘镜像:发布组带 @,IMAX/USA 地区标签不进入标题
    m = parse_filename("Project Hail Mary 2026 IMAX 2160p USA UHD Blu-ray DoVi HDR10 HEVC TrueHD 7.1-Thor@HDSky.iso")
    assert m.title == "Project Hail Mary", m.title
    assert m.year == 2026
    assert m.group == "Thor@HDSky", m.group
    assert m.source == "BluRay"
    assert m.video_codec == "hevc"
    assert "IMAX" not in m.title and "USA" not in m.title and "Thor" not in m.title


def test_us_movie_preserved():
    # 区分大小写:片名 "Us"(2019)不能被地区标签规则误删
    m = parse_filename("Us.2019.1080p.BluRay.x264-SPARKS.mkv")
    assert m.title == "Us", m.title
    assert m.group == "SPARKS"


def test_imax_dropped_in_title():
    m = parse_filename("Dunkirk.2017.IMAX.1080p.BluRay.x264-GROUP.mkv")
    assert m.title == "Dunkirk", m.title
    assert "IMAX" not in m.title


def test_channel_prefix_noise_cleaned():
    m = parse_filename("[中央广播电视总台4K超高清频道 舌尖上的中国 第四季].CCTV-4K.A.Bite.of.China.2025.S04.2160p.50fps.UHDTV.HEVC.10bit.HLG.DD5.1-QHstudIo.ts")
    assert m.season == 4
    assert m.year == 2025
    assert m.source == "UHDTV"
    assert m.video_codec == "hevc"
    assert "fps" not in m.title.lower() and "hlg" not in m.title.lower()
    assert "第四季" not in m.title
    assert m.group == "QHstudIo"

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
