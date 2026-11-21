"""法條參考：依 references/laws.json，從全國法規資料庫取得條文並快取。

主要來源是全國法規資料庫 OpenAPI 的整批下載（https://law.moj.gov.tw/api/ch/law/json，ZIP 內為 JSON）；
整批資料缺少某條時，再改抓單條頁面（LawSingle.aspx）。條文原文不手寫在程式庫裡。
抓取失敗時沿用舊快取；快取也沒有時只提供條號與連結，並要求 AI 不得自行補寫條文內容。
"""
from __future__ import annotations

import html
import io
import json
import re
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "references" / "laws.json"
CACHE = ROOT / "data" / "laws_cache.json"
TZ = ZoneInfo("Asia/Taipei")
UA = "ly-analysis/1.0 (+https://github.com/baize404/ly-analysis)"
BULK_URLS = ["https://law.moj.gov.tw/api/ch/law/json", "https://law.moj.gov.tw/api/ch/order/json"]
SINGLE_URL = "https://law.moj.gov.tw/LawClass/LawSingle.aspx"
ALL_URL = "https://law.moj.gov.tw/LawClass/LawAll.aspx"
ARTICLE_NO = re.compile(r"第\s*(\d+)\s*(?:-\s*(\d+))?\s*條")


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def article_url(pcode: str | None, no: str) -> str | None:
    if not pcode:
        return None
    return SINGLE_URL + "?" + urllib.parse.urlencode({"pcode": pcode, "flno": no})


def law_url(pcode: str | None) -> str | None:
    return f"{ALL_URL}?pcode={pcode}" if pcode else None


def article_label(no: str) -> str:
    main, _, sub = no.partition("-")
    return f"第 {main} 條" + (f"之 {sub}" if sub else "")


def normalize_no(article_no: str) -> str | None:
    """「第 71-1 條」→「71-1」。"""
    m = ARTICLE_NO.search(article_no or "")
    if not m:
        return None
    return m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")


def _get(url: str, timeout: int = 300) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------- 整批下載（OpenAPI） ----------

def parse_bulk(raw: bytes) -> tuple[dict, str | None]:
    """解析 OpenAPI 下載的 ZIP（或直接是 JSON）。回傳 ({法規名稱: 法規資料}, 資料更新日)。"""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".json"))
            raw = zf.read(name)
    data = json.loads(raw.decode("utf-8-sig"))
    laws = data.get("Laws") if isinstance(data, dict) else data
    if not isinstance(laws, list):
        raise ValueError(f"無法辨識的資料格式，最上層欄位：{list(data)[:10] if isinstance(data, dict) else type(data)}")
    index = {}
    for law in laws:
        name = (law.get("LawName") or "").strip()
        if name:
            index[name] = law
    updated = data.get("UpdateDate") if isinstance(data, dict) else None
    return index, updated


def law_record(law: dict) -> dict:
    url = law.get("LawURL") or ""
    pcode = (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("pcode") or [None])[0]
    articles = {}
    for art in law.get("LawArticles") or []:
        if (art.get("ArticleType") or "A") != "A":
            continue  # C 為章節標題
        no = normalize_no(art.get("ArticleNo") or "")
        if no:
            articles[no] = (art.get("ArticleContent") or "").strip()
    return {
        "pcode": pcode,
        "level": law.get("LawLevel"),
        "modified": law.get("LawModifiedDate"),
        "abandoned": bool((law.get("LawAbandonNote") or "").strip()),
        "articles": articles,
    }


# ---------- 單條頁面（備援） ----------

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
    return _strip(m.group(1)) or None


def fetch_single(pcode: str, no: str) -> str | None:
    return parse_article(_get(article_url(pcode, no), timeout=60).decode("utf-8", errors="replace"))


# ---------- 快取 ----------

def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {"fetched_at": None, "laws": {}, "failures": []}


def refresh(max_age_days: int = 7, force: bool = False, log=print) -> dict:
    """快取超過 max_age_days 或設定有變動才重新下載。回傳快取。"""
    cfg = load_config()
    cache = load_cache()
    wanted = {law["name"]: law["articles"] for law in cfg["laws"]}
    have = {name: list(info.get("articles", {})) for name, info in cache.get("laws", {}).items()}
    config_changed = any(set(arts) - set(have.get(name, [])) for name, arts in wanted.items())
    if cache.get("fetched_at") and not force and not config_changed:
        age = datetime.now(TZ) - datetime.fromisoformat(cache["fetched_at"])
        if age < timedelta(days=max_age_days):
            return cache

    failures: list[str] = []
    found: dict[str, dict] = {}
    updated_dates = []
    for url in BULK_URLS:
        if all(name in found for name in wanted):
            break
        try:
            log(f"下載全國法規資料庫整批資料：{url}")
            index, updated = parse_bulk(_get(url))
            updated_dates.append(updated)
            for name in wanted:
                if name not in found and name in index:
                    found[name] = law_record(index[name])
        except Exception as exc:
            failures.append(f"整批下載失敗（{url}）：{exc}")

    old = cache.get("laws", {})
    new_laws: dict[str, dict] = {}
    for law in cfg["laws"]:
        name = law["name"]
        rec = found.get(name)
        old_rec = old.get(name, {})
        pcode = (rec or {}).get("pcode") or old_rec.get("pcode") or law.get("pcode")
        entry = {
            "pcode": pcode,
            "url": law_url(pcode),
            "modified": (rec or {}).get("modified") or old_rec.get("modified"),
            "abandoned": (rec or {}).get("abandoned", False),
            "reason": law.get("reason"),
            "articles": {},
        }
        if rec is None:
            failures.append(f"整批資料中找不到「{name}」")
        for no in law["articles"]:
            text = (rec or {}).get("articles", {}).get(no)
            source = "openapi"
            if not text and pcode:
                try:
                    text = fetch_single(pcode, no)
                    source = "single"
                    time.sleep(0.3)
                except Exception as exc:
                    failures.append(f"{name}{article_label(no)} 單條抓取失敗：{exc}")
            if not text and old_rec.get("articles", {}).get(no):
                text = old_rec["articles"][no]["text"]
                source = "cache"
                failures.append(f"{name}{article_label(no)} 本次未取得，沿用上次的快取")
            if text:
                entry["articles"][no] = {"text": text, "source": source}
            else:
                failures.append(f"{name}{article_label(no)} 無法取得")
        new_laws[name] = entry

    cache = {
        "fetched_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "database_updated": next((d for d in updated_dates if d), None),
        "laws": new_laws,
        "failures": failures,
    }
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(len(v["articles"]) for v in new_laws.values())
    log(f"法條：取得 {total} 條" + (f"，{len(failures)} 項問題：" + "；".join(failures[:5]) if failures else ""))
    return cache


def reference_text(cache: dict | None = None) -> str:
    """給 AI 的法條參考區塊。"""
    cache = cache if cache is not None else load_cache()
    cfg = load_config()
    stamp = cache.get("database_updated") or (cache.get("fetched_at") or "")[:10]
    out = [
        "以下條文取自全國法規資料庫" + (f"（資料版本 {stamp}）" if stamp else "") + "。",
        "標示「原文未取得」的條文，只能提到條號，不得自行寫出或推測條文內容。",
        "",
    ]
    for law in cfg["laws"]:
        info = cache.get("laws", {}).get(law["name"], {})
        title = f"## {law['name']}"
        if info.get("modified"):
            title += f"（最新異動 {info['modified']}）"
        out.append(title)
        for no in law["articles"]:
            item = info.get("articles", {}).get(no)
            body = item["text"] if item else "（原文未取得）"
            link = article_url(info.get("pcode"), no)
            out.append(f"### {law['name']} {article_label(no)}\n{body}" + (f"\n來源：{link}" if link else "") + "\n")
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
