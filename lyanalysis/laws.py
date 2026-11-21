"""法條參考：向全國法規資料庫抓取 references/laws.json 列出的條文，並快取。

條文原文不手寫在程式庫裡，避免抄錯或過時。抓取失敗時沿用快取；
快取也沒有時只提供條號與連結，並要求 AI 不得自行補寫條文內容。
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "references" / "laws.json"
CACHE = ROOT / "data" / "laws_cache.json"
TZ = ZoneInfo("Asia/Taipei")
UA = "ly-analysis/1.0 (+https://github.com/baize404/ly-analysis)"
SINGLE_URL = "https://law.moj.gov.tw/LawClass/LawSingle.aspx"


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def article_url(pcode: str, no: str) -> str:
    return SINGLE_URL + "?" + urllib.parse.urlencode({"pcode": pcode, "flno": no})


def article_label(no: str) -> str:
    main, _, sub = no.partition("-")
    return f"第 {main} 條" + (f"之 {sub}" if sub else "")


def _strip(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</div>", "\n", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def parse_article(page: str) -> str | None:
    """從 LawSingle 頁面取出條文內容（law-article 區塊）。"""
    m = re.search(r'<div class="law-article">(.*?)</div>\s*</div>', page, flags=re.S)
    if not m:
        return None
    text = _strip(m.group(1))
    return text or None


def fetch_article(pcode: str, no: str) -> str | None:
    req = urllib.request.Request(article_url(pcode, no), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    return parse_article(page)


def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {"fetched_at": None, "articles": {}}


def refresh(max_age_days: int = 7, force: bool = False, log=print) -> dict:
    """快取超過 max_age_days 才重新抓。回傳快取內容。"""
    cache = load_cache()
    fetched = cache.get("fetched_at")
    if fetched and not force:
        age = datetime.now(TZ) - datetime.fromisoformat(fetched)
        if age < timedelta(days=max_age_days):
            return cache
    cfg = load_config()
    failures = []
    for law in cfg["laws"]:
        for no in law["articles"]:
            key = f"{law['pcode']}:{no}"
            try:
                text = fetch_article(law["pcode"], no)
            except Exception as exc:  # 網路錯誤：沿用舊快取
                failures.append(f"{law['name']}{article_label(no)}（{exc}）")
                continue
            if text:
                cache["articles"][key] = {"law": law["name"], "no": no, "text": text}
            else:
                failures.append(f"{law['name']}{article_label(no)}（頁面格式無法解析）")
            time.sleep(0.3)
    cache["fetched_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    cache["failures"] = failures
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failures:
        log(f"⚠ 法條抓取失敗 {len(failures)} 條：" + "；".join(failures[:5]))
    return cache


def reference_text(cache: dict | None = None) -> str:
    """給 AI 的法條參考區塊。"""
    cache = cache if cache is not None else load_cache()
    cfg = load_config()
    out = [
        "以下條文由程式自全國法規資料庫抓取" + (f"（{cache['fetched_at'][:10]}）" if cache.get("fetched_at") else "") + "。",
        "標示「原文未取得」的條文，只能提到條號，不得自行寫出或推測條文內容。",
        "",
    ]
    for law in cfg["laws"]:
        out.append(f"## {law['name']}")
        for no in law["articles"]:
            item = cache.get("articles", {}).get(f"{law['pcode']}:{no}")
            body = item["text"] if item else "（原文未取得，請參考連結）"
            out.append(f"### {law['name']} {article_label(no)}\n{body}\n來源：{article_url(law['pcode'], no)}\n")
    notes = ROOT / "references" / "notes.md"
    if notes.exists():
        out.append(notes.read_text(encoding="utf-8"))
    return "\n".join(out)


def allowed_citations() -> tuple[dict[str, set[str]], set[str]]:
    """lint 使用：{法律名稱或簡稱: {條號}}，以及可引用的釋字號。"""
    cfg = load_config()
    allowed: dict[str, set[str]] = {}
    for law in cfg["laws"]:
        nos = set(law["articles"])
        for name in [law["name"], *law.get("short", [])]:
            allowed.setdefault(name, set()).update(nos)
    return allowed, set(cfg.get("interpretations", []))
