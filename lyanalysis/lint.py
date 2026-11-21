"""不靠 AI 的程式檢查：抓出分析稿中可能偏頗的用語，以及未標「需查證」的法條引用。

這是保守的機械檢查，會有誤判，結果只用來在網頁上提示讀者與維護者，不會自動改稿。
"""
from __future__ import annotations

import re

from .laws import allowed_citations

# 帶有褒貶色彩、應只出現在引號內（轉述他人原話）的詞。
LOADED_TERMS = [
    "強行", "強推", "硬闖", "霸凌", "護航", "荒謬", "荒唐", "德政", "惡法", "毀憲", "亂政", "濫權",
    "奧步", "鬧劇", "黑箱", "暴衝", "背骨", "賣台", "舔共", "綠共", "藍白合流", "芭樂票", "撒錢", "買票",
    "憲政哥吉拉", "薪水小偷", "債留子孫", "還稅於民",
]

QUOTED = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"\n]*\"")
CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
NUM = r"[0-9０-９零〇一二兩三四五六七八九十百]+"
ARTICLE = re.compile(rf"([一-鿿]{{1,14}}?(?:法|條文|條例))\s*第\s*({NUM})\s*條(?:\s*之\s*({NUM}))?")
INTERP = re.compile(rf"釋字(?:第)?\s*({NUM})\s*號")
VERIFY_WINDOW = 40


def cn2int(text: str) -> int | None:
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if text.isdigit():
        return int(text)
    total, current = 0, 0
    for ch in text:
        if ch in CN_DIGIT:
            current = CN_DIGIT[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def _unquoted(text: str) -> str:
    return QUOTED.sub(lambda m: "　" * len(m.group(0)), text)


def _flagged_nearby(text: str, end: int) -> bool:
    return "需查證" in text[end:end + VERIFY_WINDOW]


def _law_key(raw: str, allowed: dict[str, set[str]]) -> str | None:
    for name in sorted(allowed, key=len, reverse=True):
        if raw.endswith(name):
            return name
    return None


def check(markdown: str) -> list[str]:
    issues: list[str] = []
    plain = _unquoted(markdown)

    for term in LOADED_TERMS:
        count = plain.count(term)
        if count:
            issues.append(f"引號外出現帶有立場色彩的用語「{term}」（{count} 處）")

    allowed, interps = allowed_citations()
    seen = set()
    for m in ARTICLE.finditer(markdown):
        raw, main, sub = m.group(1), cn2int(m.group(2)), m.group(3)
        if main is None:
            continue
        no = f"{main}-{cn2int(sub)}" if sub else str(main)
        key = _law_key(raw, allowed)
        if key and no in allowed[key]:
            continue
        if _flagged_nearby(markdown, m.end()):
            continue
        label = f"{key or raw}第{main}條" + (f"之{cn2int(sub)}" if sub else "")
        if label not in seen:
            seen.add(label)
            issues.append(f"引用了法條參考以外的「{label}」，但未標註「需查證」")

    for m in INTERP.finditer(markdown):
        no = cn2int(m.group(1))
        if no is None or str(no) in interps or _flagged_nearby(markdown, m.end()):
            continue
        label = f"釋字第{no}號"
        if label not in seen:
            seen.add(label)
            issues.append(f"引用了參考資料以外的「{label}」，但未標註「需查證」")
    return issues
