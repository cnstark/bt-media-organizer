"""地区分类规则测试(离线)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.recognize.tmdb import DEFAULT_REGION_CATEGORIES, region_category  # noqa: E402


def test_region_category():
    assert region_category(["JP"], DEFAULT_REGION_CATEGORIES) == "日韩剧"
    assert region_category(["US"], DEFAULT_REGION_CATEGORIES) == "欧美剧"
    assert region_category(["GB"], DEFAULT_REGION_CATEGORIES) == "欧美剧"
    assert region_category(["CN"], DEFAULT_REGION_CATEGORIES) == "国产剧"
    assert region_category(["TW"], DEFAULT_REGION_CATEGORIES) == "港台剧"
    assert region_category(["KR"], DEFAULT_REGION_CATEGORIES) == "日韩剧"
    assert region_category([], DEFAULT_REGION_CATEGORIES) is None
    assert region_category(None, DEFAULT_REGION_CATEGORIES) is None
    assert region_category(["ZZ"], DEFAULT_REGION_CATEGORIES) is None


def test_region_category_multi_country():
    # 合拍:按规则顺序首个命中即止(默认规则里"欧美剧"在前)
    assert region_category(["CN", "US"], DEFAULT_REGION_CATEGORIES) == "欧美剧"
    assert region_category(["CN", "JP"], DEFAULT_REGION_CATEGORIES) == "国产剧"
    assert region_category(["JP", "KR"], DEFAULT_REGION_CATEGORIES) == "日韩剧"
    assert region_category(["HK", "TW"], DEFAULT_REGION_CATEGORIES) == "港台剧"
    assert region_category(["TH", "SG"], DEFAULT_REGION_CATEGORIES) == "亚洲剧"


def test_custom_rules_override():
    rules = {"国产剧": ["CN"], "泰国剧": ["TH"]}
    assert region_category(["TH"], rules) == "泰国剧"
    assert region_category(["US"], rules) is None
    assert region_category(["CN"], rules) == "国产剧"


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
