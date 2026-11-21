#!/usr/bin/env python3
"""每日排程入口：抓會議資料 → AI 分析 → 程式檢查 → 產生網站。

用法：
  python run.py                      # 最近 lookback_days 天
  python run.py --since 2026-11-20   # 指定起始日期
  python run.py --only-site          # 只重新產生網站
  python run.py --no-analyze         # 只抓資料，不呼叫 AI

有錯誤時 exit code 為 1，並把中文的錯誤原因說明寫進 GitHub Actions 的執行摘要。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lyanalysis import collect, laws, lint, lyapi, site
from lyanalysis.transcript import build_roster

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MEETINGS = DATA / "meetings"
DOCS = ROOT / "docs"
TZ = ZoneInfo("Asia/Taipei")

errors: list[dict] = []
notes: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def explain(exc: BaseException) -> str:
    """把常見錯誤翻成看得懂的原因與處理方式。"""
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    if name == "AuthenticationError":
        return "Anthropic API 金鑰無效。請到 repo 的 Settings → Secrets and variables → Actions，確認 ANTHROPIC_API_KEY 是否貼對、是否已被撤銷。"
    if name == "PermissionDeniedError":
        return "API 金鑰沒有使用這個模型的權限。請確認帳戶狀態，或在 config.json 改用其他模型。"
    if name == "NotFoundError":
        return "找不到指定的模型。請檢查 config.json 的 model／fallback_model 名稱是否正確。"
    if name == "RateLimitError":
        return "超過 API 速率限制或用量上限。下次排程會自動重試；若持續發生，請到 Anthropic Console 查看用量限制。"
    if name == "BadRequestError" and ("credit" in low or "balance" in low or "billing" in low):
        return "Anthropic API 帳戶餘額不足。請到 Anthropic Console 的 Billing 儲值後再執行。"
    if name == "BadRequestError":
        return f"API 拒絕了請求格式（{text[:200]}）。可能是設定的 max_tokens 超過模型上限，或逐字稿過長。"
    if name in ("APIConnectionError", "APITimeoutError"):
        return "連不上 Anthropic API（網路逾時或中斷）。通常是暫時性問題，下次排程會重試。"
    if name in ("InternalServerError", "OverloadedError") or "overloaded" in low or "529" in low:
        return "Anthropic 伺服器忙碌或暫時故障。下次排程會重試。"
    if name == "Refusal":
        return f"模型的安全機制婉拒了這次分析（{text}），且備用模型也無法完成。這是平台端的判斷，不是程式錯誤；可在 config.json 換用其他 fallback_model。"
    if name == "LYAPIError":
        return f"LYAPI（立法院資料）連線失敗：{text[:200]}。可能是對方服務暫時中斷，下次排程會重試。"
    if name in ("URLError", "TimeoutError", "ConnectionError"):
        return f"網路連線失敗：{text[:200]}。通常是暫時性問題。"
    return f"未預期的錯誤 {name}：{text[:300]}（完整訊息見執行紀錄）"


def record_error(stage: str, target: str, exc: BaseException) -> dict:
    item = {"stage": stage, "target": target, "type": type(exc).__name__,
            "message": str(exc)[:500], "explain": explain(exc),
            "at": datetime.now(TZ).isoformat(timespec="seconds")}
    errors.append(item)
    log(f"✗ [{stage}] {target}：{item['explain']}")
    traceback.print_exc()
    return item


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def date_range(since: date, until: date) -> list[str]:
    days = (until - since).days
    return [(since + timedelta(days=i)).isoformat() for i in range(days + 1)]


def get_roster(cfg: dict) -> dict:
    cache = DATA / "legislators.json"
    try:
        legislators = lyapi.list_legislators(cfg["term"])
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(legislators, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except Exception as exc:
        record_error("立委名單", "LYAPI /legislators", exc)
        legislators = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else []
    return build_roster(legislators)


def collect_all(cfg: dict, since: date, until: date) -> None:
    roster = get_roster(cfg)
    seen: set[str] = set()
    for day in date_range(since, until):
        try:
            ivods = lyapi.list_ivods(day)
        except Exception as exc:
            record_error("抓會議清單", day, exc)
            continue
        groups = collect.group_ivods(ivods, cfg["meeting_types"])
        log(f"{day}：{len(ivods)} 支影片，{len(groups)} 場符合條件的會議")
        for group in groups:
            try:
                meta = collect.collect_group(group, roster, MEETINGS)
                seen.add(meta["slug"])
                log(f"  · {meta['slug']}：{meta['source_label']}")
            except Exception as exc:
                record_error("抓會議內容", f"{group['code']} {group['date']}", exc)

    # 較早的會議：公報刊出後改用公報重新整理
    cutoff = (until - timedelta(days=int(cfg["gazette_recheck_days"]))).isoformat()
    for path in sorted(MEETINGS.glob("*/meta.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta["slug"] in seen or meta.get("source") == "gazette" or meta.get("date", "") < cutoff:
            continue
        try:
            if collect.gazette_ready(meta):
                new = collect.collect_group(collect.regroup_from_meta(meta), roster, MEETINGS)
                log(f"  · {new['slug']}：公報已刊出，改用公報發言紀錄")
        except Exception as exc:
            record_error("檢查公報", meta["slug"], exc)


def needs_analysis(meta: dict, cfg: dict) -> bool:
    if meta.get("source", "none") == "none":
        return False
    done = meta.get("analysis")
    if not done:
        return True
    if meta.get("source_rank", 0) > done.get("source_rank", 0):
        return True  # 來源升級（例如公報刊出）
    grown = meta.get("transcript_chars", 0) >= done.get("transcript_chars", 0) * float(cfg["reanalyze_growth_ratio"])
    return meta.get("transcript_sha256") != done.get("transcript_sha256") and grown


def analyze_all(cfg: dict, limit: int) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        notes.append("未設定 ANTHROPIC_API_KEY，本次只更新資料與網站，沒有產生分析。請到 repo 的 Settings → Secrets and variables → Actions 新增。")
        log("⚠ " + notes[-1])
        return
    import anthropic
    from lyanalysis.analyze import analyze

    client = anthropic.Anthropic(max_retries=4)
    try:
        cache = laws.refresh(int(cfg["laws_refresh_days"]), log=log)
        if cache.get("failures"):
            notes.append(f"有 {len(cache['failures'])} 條法條無法從全國法規資料庫取得，分析時這些條文只提供條號。")
    except Exception as exc:
        record_error("抓法條", "全國法規資料庫", exc)

    metas = [json.loads(p.read_text(encoding="utf-8")) for p in MEETINGS.glob("*/meta.json")]
    todo = sorted((m for m in metas if needs_analysis(m, cfg)), key=lambda m: m["date"], reverse=True)
    if len(todo) > limit:
        notes.append(f"待分析 {len(todo)} 場，本次依設定只處理最新的 {limit} 場，其餘留待下次。")
    for meta in todo[:limit]:
        folder = MEETINGS / meta["slug"]
        transcript = (folder / "transcript.txt").read_text(encoding="utf-8")
        log(f"分析 {meta['slug']}（{meta['transcript_chars']:,} 字）…")
        try:
            text, record = analyze(meta, transcript, cfg, client)
        except Exception as exc:
            meta["analysis_error"] = record_error("AI 分析", meta["slug"], exc)
            collect.save_meta(folder, meta)
            continue
        (folder / "analysis.md").write_text(text + "\n", encoding="utf-8")
        meta["analysis"] = {
            "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "source": meta["source"], "source_rank": meta["source_rank"], "source_label": meta["source_label"],
            "transcript_sha256": meta["transcript_sha256"], "transcript_chars": meta["transcript_chars"],
            "lint": lint.check(text), **record,
        }
        meta.pop("analysis_error", None)
        collect.save_meta(folder, meta)
        usage = record.get("usage", {})
        log(f"  ✓ 完成：{record['model']}，輸入 {usage.get('input_tokens', 0):,}、輸出 {usage.get('output_tokens', 0):,} tokens，"
            f"程式檢查提示 {len(meta['analysis']['lint'])} 項")


def write_summary(cfg: dict) -> None:
    lines = ["## 立法院會議分析：執行結果", ""]
    if errors:
        lines += [f"### ✗ 發生 {len(errors)} 個錯誤", "", "| 階段 | 對象 | 原因與處理方式 |", "|---|---|---|"]
        lines += [f"| {e['stage']} | {e['target']} | {e['explain'].replace('|', '／')} |" for e in errors]
        lines.append("")
    else:
        lines += ["### ✓ 沒有錯誤", ""]
    if notes:
        lines += ["### 備註", ""] + [f"- {n}" for n in notes] + [""]
    lines.append(f"網站：{cfg.get('site_url')}")
    text = "\n".join(lines) + "\n"
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(text)
    DATA.mkdir(exist_ok=True)
    (DATA / "last_run.json").write_text(json.dumps(
        {"at": datetime.now(TZ).isoformat(timespec="seconds"), "errors": errors, "notes": notes},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--until", default="", help="結束日期 YYYY-MM-DD（預設今天）")
    parser.add_argument("--max", default="", help="本次最多分析幾場")
    parser.add_argument("--no-analyze", action="store_true")
    parser.add_argument("--only-site", action="store_true")
    args = parser.parse_args()
    cfg = load_config()

    if not args.only_site:
        today = datetime.now(TZ).date()
        until = date.fromisoformat(args.until) if args.until.strip() else today
        since = date.fromisoformat(args.since) if args.since.strip() else until - timedelta(days=int(cfg["lookback_days"]))
        if since > until:
            print("起始日期不能晚於結束日期", file=sys.stderr)
            return 2
        log(f"範圍：{since} ～ {until}，會議種類：{'、'.join(cfg['meeting_types'])}")
        collect_all(cfg, since, until)
        if not args.no_analyze:
            limit = int(args.max) if args.max.strip() else int(cfg["max_analyses_per_run"])
            analyze_all(cfg, limit)

    count = site.build(MEETINGS, DOCS, cfg)
    log(f"網站已產生：{count} 場會議 → {DOCS}")
    if not args.only_site:
        write_summary(cfg)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
