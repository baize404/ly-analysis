"""從 LYAPI 抓會議、選出最好的文字來源，存到 data/meetings/<slug>/。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import lyapi
from .transcript import ivod_lines, render, speech_lines

TZ = ZoneInfo("Asia/Taipei")

TYPE_SLUG = {"院會": "plenary", "委員會": "committee", "黨團協商": "caucus", "全院委員會": "whole", "公聽會": "hearing"}

# 文字來源的可信度排序：數字越大越可靠。來源升級時才重新分析，避免重複花費。
SOURCE_RANK = {"none": 0, "ai_clips": 1, "ai_full": 2, "gazette": 3}
SOURCE_LABEL = {
    "none": "尚無逐字稿",
    "ai_clips": "AI 語音辨識逐字稿（委員發言片段，未經人工校對）",
    "ai_full": "AI 語音辨識逐字稿（完整會議影片，未經人工校對）",
    "gazette": "立法院公報發言紀錄（官方逐字紀錄）",
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def slug_for(code: str, date: str, kind: str) -> str:
    digits = "-".join(re.findall(r"\d+", code)) or hashlib.sha1(code.encode()).hexdigest()[:8]
    return f"{date}-{TYPE_SLUG.get(kind, 'other')}-{digits}"


def group_ivods(ivods: list[dict], types: list[str]) -> list[dict]:
    """依（會議代碼, 日期）分組。同一個會議代碼可能橫跨好幾天，每天各算一場。"""
    groups: dict[tuple[str, str], dict] = {}
    for iv in ivods:
        md = iv.get("會議資料") or {}
        kind = md.get("種類") or ""
        code, date = md.get("會議代碼"), iv.get("日期")
        if not code or not date or (types and kind not in types):
            continue
        group = groups.setdefault((code, date), {
            "code": code,
            "date": date,
            "kind": kind,
            "title": md.get("標題") or md.get("會議代碼:str") or code,
            "committees": md.get("委員會代碼:str") or [],
            "ivods": [],
        })
        group["ivods"].append(iv)
    return list(groups.values())


def meeting_info(code: str, date: str) -> dict:
    info = {"ppg_url": None, "reason": None, "agenda": [], "third_reading": []}
    try:
        meet = lyapi.get_meet(code)
    except lyapi.LYAPIError:
        return info
    info["third_reading"] = meet.get("三讀法案") or []
    for d in meet.get("會議資料") or []:
        if d.get("日期") == date:
            info["ppg_url"] = d.get("ppg_url")
            info["reason"] = d.get("會議事由")
    seen = set()
    for web in meet.get("議事網資料") or []:
        for bill in ((web.get("關係文書") or {}).get("議案") or []):
            title = bill.get("標題")
            if bill.get("類別") == "討論事項" and title and title not in seen:
                seen.add(title)
                info["agenda"].append(title)
    return info


def _transcribed(ivods: list[dict], kind: str) -> list[dict]:
    return [v for v in ivods if v.get("影片種類") == kind and "ai-transcript" in (v.get("支援功能") or [])]


def best_source(group: dict, roster: dict) -> tuple[str, list[dict], dict]:
    """依序嘗試：公報發言紀錄 → 完整會議 AI 逐字稿 → 委員發言片段 AI 逐字稿。"""
    try:
        speeches = lyapi.list_speeches(group["code"], group["date"])
    except lyapi.LYAPIError:
        speeches = []
    if speeches:
        gazettes = sorted({str(s.get("公報編號")) for s in speeches if s.get("公報編號")})
        return "gazette", speech_lines(speeches, roster), {"gazette_ids": gazettes}

    ivods = sorted(group["ivods"], key=lambda v: v.get("開始時間") or "")
    for source, items in (("ai_full", _transcribed(ivods, "Full")), ("ai_clips", _transcribed(ivods, "Clip"))):
        lines: list[dict] = []
        for iv in items:
            try:
                detail = lyapi.get_ivod(iv["IVOD_ID"])
            except lyapi.LYAPIError:
                continue
            lines.extend(ivod_lines(detail, roster))
        if lines:
            return source, lines, {}
    return "none", [], {}


def _ivod_summary(iv: dict) -> dict:
    return {
        "id": iv.get("IVOD_ID"),
        "kind": iv.get("影片種類"),
        "speaker": iv.get("委員名稱"),
        "url": iv.get("IVOD_URL"),
        "start": iv.get("開始時間"),
        "end": iv.get("結束時間"),
    }


def load_meta(folder: Path) -> dict | None:
    path = folder / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_meta(folder: Path, meta: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect_group(group: dict, roster: dict, data_dir: Path) -> dict:
    slug = slug_for(group["code"], group["date"], group["kind"])
    folder = data_dir / slug
    old = load_meta(folder) or {}
    source, lines, extra = best_source(group, roster)

    meta = {
        **old,
        "slug": slug,
        "code": group["code"],
        "date": group["date"],
        "kind": group["kind"],
        "title": group["title"],
        "committees": group.get("committees") or [],
        "name": next((iv.get("會議名稱") for iv in group["ivods"] if iv.get("影片種類") == "Full"),
                     group["ivods"][0].get("會議名稱") if group["ivods"] else None),
        "ivods": [_ivod_summary(iv) for iv in sorted(group["ivods"], key=lambda v: v.get("開始時間") or "")],
        **meeting_info(group["code"], group["date"]),
        "checked_at": now_iso(),
    }

    # 只有在來源不比之前差時才覆寫逐字稿（例如 AI 逐字稿暫時抓不到時，保留舊的）
    if SOURCE_RANK[source] >= SOURCE_RANK.get(old.get("source", "none"), 0) and lines:
        text = render(lines)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "transcript.txt").write_text(text + "\n", encoding="utf-8")
        meta.update({
            "source": source,
            "source_rank": SOURCE_RANK[source],
            "source_label": SOURCE_LABEL[source],
            "source_extra": extra,
            "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "transcript_chars": len(text),
        })
    meta.setdefault("source", "none")
    meta.setdefault("source_rank", 0)
    meta.setdefault("source_label", SOURCE_LABEL["none"])
    meta.setdefault("analysis", None)
    save_meta(folder, meta)
    return meta


def gazette_ready(meta: dict) -> bool:
    try:
        return lyapi.count_speeches(meta["code"], meta["date"]) > 0
    except lyapi.LYAPIError:
        return False


def regroup_from_meta(meta: dict) -> dict:
    """公報刊出後重新整理時使用：用已存的資料重建 group。"""
    ivods = []
    for iv in meta.get("ivods") or []:
        ivods.append({
            "IVOD_ID": iv.get("id"), "影片種類": iv.get("kind"), "委員名稱": iv.get("speaker"),
            "IVOD_URL": iv.get("url"), "開始時間": iv.get("start"), "結束時間": iv.get("end"),
            "會議名稱": meta.get("name"),
        })
    return {"code": meta["code"], "date": meta["date"], "kind": meta["kind"], "title": meta["title"],
            "committees": meta.get("committees") or [], "ivods": ivods}
