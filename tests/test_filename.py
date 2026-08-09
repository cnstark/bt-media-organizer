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


def test_audio_glue_and_version_tags():
    # TrueHD7.1 拆开后的 TrueHD7 / 2Audio / DTS-X / MA / Complete / EDR / IQ / DDP2 等不进入标题
    m = parse_filename("Thor.Ragnarok.2017.UHD.BluRay.REMUX.2160p.HEVC.Atmos.TrueHD7.1.2Audio-CHD.mkv")
    assert m.title == "Thor Ragnarok", m.title
    assert m.group == "CHD"
    m2 = parse_filename("Black.Widow.2021.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.TrueHD.7.1.Atmos-FGT.mkv")
    assert m2.title == "Black Widow", m2.title
    m3 = parse_filename("[我的阿勒泰].To.the.Wonder.2024.S01.Complete.2160p.WEB-DL.EDR.H265.DDP5.1.Atmos-UBWEB.mkv")
    assert m3.title == "To the Wonder", m3.title
    m4 = parse_filename("Meet.Yourself.S01.2023.2160p.IQ.WEB-DL.H265.DDP2.0-HHWEB.mkv")
    assert m4.title == "Meet Yourself", m4.title
    assert "MA" not in m2.title and "IQ" not in m4.title and "Complete" not in m3.title


def test_collection_pack_stripped():
    m = parse_filename("Harry.Potter.8-Film.Collection.2001-2011.UHD.BluRay.2160p.DTS-X.7.1.HDR.x265.10bit.mkv")
    assert m.title == "Harry Potter", m.title
    assert m.year == 2011  # 最后一个年份(与 _find_year 取末尾语义一致)
    assert "Collection" not in m.title and "2001-2011" not in m.title


def test_sequel_number_kept():
    # 序号紧跟标题词时保留(Expendables 3),声道数字仍丢弃(TrueHD 7.1)
    m = parse_filename("The.Expendables.3.2014.Theatrical.Cut.BluRay.2160p.UHD.REMUX.HEVC.TrueHD.7.1-UBi.mkv")
    assert m.title == "The Expendables 3", m.title
    m2 = parse_filename("Movie.2020.1080p.TrueHD.7.1.mkv")
    assert m2.title == "Movie", m2.title
    assert "7" not in m2.title and "1" not in m2.title


def test_cn_version_suffix_stripped():
    m = parse_filename("泰坦尼克号白星版.Titanic.1997.Extended.Fan.Cut.1080p.BluRay.DTS.x264-QNY.mkv")
    assert "白星版" not in m.title
    m2 = parse_filename("银翼杀手(最终剪辑版).1982.1080p.国英双语.中英字幕￡CMCT风潇潇.mkv")
    assert m2.title == "银翼杀手", m2.title
    assert "最终剪辑版" not in m2.title and "国英双语" not in m2.title


def test_ma_iq_case_sensitive():
    # 全大写 MA/IQ 非标题首位才丢弃;片名 Ma(2019) 与 I.Q.(1994) 保留
    m = parse_filename("Ma.2019.1080p.BluRay.x264-SPARKS.mkv")
    assert m.title == "Ma", m.title
    m2 = parse_filename("I.Q.1994.1080p.BluRay.x264-GROUP.mkv")
    assert "I" in m2.title and "Q" in m2.title


def test_dual_year_mid_title_dropped():
    # Blade Runner 2049 2017:检测年份取最后一个(2017),中段 2049 丢弃,标题剩 Blade Runner
    m = parse_filename("Blade.Runner.2049.2017.UHD.BluRay.REMUX.2160p.HEVC.Atmos.TrueHD7.1.2Audio-CHD.mkv")
    assert m.title == "Blade Runner", m.title
    assert m.year == 2017


def test_title_year_at_start_kept():
    # 开头年份是片名(2001/1917),不能丢
    m = parse_filename("2001.A.Space.Odyssey.1968.1080p.BluRay.x264.mkv")
    assert m.title == "2001 A Space Odyssey", m.title
    assert m.year == 1968
    m2 = parse_filename("1917.2019.1080p.BluRay.x264-GROUP.mkv")
    assert m2.title == "1917", m2.title
    assert m2.year == 2019


def test_h264_split_h_dropped():
    m = parse_filename("Till.We.Meet.Again.2021.1080p.DSNP.WEB-DL.DDP5.1.H.264-CTRLWEB.mkv")
    assert m.title == "Till We Meet Again", m.title
    assert "H" not in m.title and "DSNP" not in m.title

def test_cn_full_season_marker():
    m = parse_filename("生活大爆炸.全12季.The.Big.Bang.Theory.Complete.1080p.Blu-Ray.AC3.x265.10bit-Yumi.mkv")
    assert "全12季" not in m.title
    assert "生活大爆炸" in m.title and "The Big Bang Theory" in m.title


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
