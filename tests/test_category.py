"""类别规则引擎测试:完全对齐 MoviePilot config/category.yaml。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.recognize.category import (  # noqa: E402
    DEFAULT_MOVIE_RULES,
    DEFAULT_TV_RULES,
    match_category,
    parse_rules,
)
from src.recognize.tmdb import MediaInfo  # noqa: E402


def _tv(genre=None, country=None, lang="", year=None):
    return MediaInfo(media_type="tv", genre_ids=genre or [], origin_country=country or [],
                     original_language=lang, year=year)


def _movie(genre=None, lang="", year=None):
    return MediaInfo(media_type="movie", genre_ids=genre or [], original_language=lang, year=year)


def test_documentary():
    """舌尖上的中国:genre 99 → 纪录片(MP 规则,纪录片在国产剧之前)。"""
    m = _tv(genre=[99], country=["CN"], lang="zh", year=2012)
    assert match_category(m, DEFAULT_TV_RULES) == "纪录片", match_category(m, DEFAULT_TV_RULES)


def test_tv_domestic_drama():
    m = _tv(genre=[18], country=["CN"], lang="zh")
    assert match_category(m, DEFAULT_TV_RULES) == "国产剧"


def test_tv_oumei():
    m = _tv(genre=[18], country=["US"], lang="en")
    assert match_category(m, DEFAULT_TV_RULES) == "欧美剧"
    m2 = _tv(genre=[18], country=["GB"], lang="en")
    assert match_category(m2, DEFAULT_TV_RULES) == "欧美剧"


def test_tv_rikorean():
    m = _tv(genre=[18], country=["JP"], lang="ja")
    assert match_category(m, DEFAULT_TV_RULES) == "日韩剧"
    m2 = _tv(genre=[18], country=["KR"], lang="ko")
    assert match_category(m2, DEFAULT_TV_RULES) == "日韩剧"


def test_tv_anime_cn_vs_jp():
    """国漫/日番:genre 16 + 地区区分,且优先于纪录片/国产剧。"""
    assert match_category(_tv(genre=[16], country=["CN"]), DEFAULT_TV_RULES) == "国漫"
    assert match_category(_tv(genre=[16], country=["JP"]), DEFAULT_TV_RULES) == "日番"


def test_tv_variety_and_kids():
    assert match_category(_tv(genre=[10764], country=["CN"]), DEFAULT_TV_RULES) == "综艺"
    assert match_category(_tv(genre=[10767], country=["US"]), DEFAULT_TV_RULES) == "综艺"
    assert match_category(_tv(genre=[10762], country=["US"]), DEFAULT_TV_RULES) == "儿童"


def test_tv_fallback():
    assert match_category(_tv(genre=[18], country=["ZZ"]), DEFAULT_TV_RULES) == "未分类"
    assert match_category(None, DEFAULT_TV_RULES) is None


def test_movie_rules():
    assert match_category(_movie(genre=[16], lang="zh"), DEFAULT_MOVIE_RULES) == "动画电影"
    assert match_category(_movie(genre=[18], lang="zh"), DEFAULT_MOVIE_RULES) == "华语电影"
    assert match_category(_movie(genre=[18], lang="en"), DEFAULT_MOVIE_RULES) == "外语电影"


def test_exclusion():
    """!值 = 排除;多条件 AND。"""
    rules = parse_rules({"非动漫": {"genre_ids": "!16"}, "其他": {}})
    assert match_category(_tv(genre=[18]), rules) == "非动漫"
    assert match_category(_tv(genre=[16]), rules) == "其他"


def test_custom_rules_and_year_range():
    rules = parse_rules({
        "十年内国产剧": {"origin_country": "CN", "release_year": "2015-2025"},
        "其他": {},
    })
    assert match_category(_tv(country=["CN"], year=2020), rules) == "十年内国产剧"
    assert match_category(_tv(country=["CN"], year=2010), rules) == "其他"


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
