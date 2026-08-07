"""路径过滤/选择/转换(IYUU 语义,纯函数,可单测)。

- 过滤 filter:命中前缀 → 排除
- 选择 selector:非空时仅保留命中前缀
- 转换 convert:eq(相等)/sub(减前缀)/add(加前缀)/replace(替换),规则按序前缀匹配
"""
from __future__ import annotations

from typing import List, Optional, Tuple

# 路径转换规则分隔符(与 IYUU 一致)
DELIMITER = "{#**#}"


def parse_rule_text(text: str) -> List[Tuple[str, str]]:
    """多行规则文本 → [(源前缀, 目标前缀)]。

    - '#' 开头为注释;空行跳过
    - 含 {#**#} 的行按分隔符拆成 (源, 目标)
    - 无分隔符的行 = (源, "") 用于 sub
    """
    rules: List[Tuple[str, str]] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if DELIMITER in line:
            parts = [p.strip() for p in line.split(DELIMITER)]
            if len(parts) == 2 and parts[0]:
                rules.append((parts[0], parts[1]))
        elif line:
            rules.append((line, ""))
    return rules


def _norm(p: str) -> str:
    """去掉尾部斜杠,统一前缀比较。"""
    return p.rstrip("/")


def convert_path(original: str, ctype: str, rules: List[Tuple[str, str]]) -> Optional[str]:
    """路径转换。失败(无规则命中/结果为空)返回 None。"""
    if ctype == "eq":
        return original or None
    for src, dst in rules:
        if not src:
            continue
        if not _norm(original).startswith(_norm(src)):
            continue
        rest = original[len(src):]
        if ctype == "sub":
            result = rest
        elif ctype == "add":
            result = dst + original
        elif ctype == "replace":
            result = dst + rest
        else:
            return None
        return result or None  # 结果为空视为映射失败
    return None


def match_path(path: str, filters: List[str], selectors: List[str]) -> bool:
    """过滤器(排除)/选择器(仅包含)判定。True = 允许转移。"""
    norm = _norm(path)
    for f in filters:
        if f and norm.startswith(_norm(f)):
            return False
    if selectors:
        for s in selectors:
            if s and norm.startswith(_norm(s)):
                return True
        return False
    return True
