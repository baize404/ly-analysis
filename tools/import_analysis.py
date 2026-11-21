#!/usr/bin/env python3
"""匯入在 API 以外撰寫的分析（例如在 Claude 對話中依同一套規則撰寫）。

用法：
  python tools/import_analysis.py <slug> <analysis.md> [--model "說明"]

會把分析存到 data/meetings/<slug>/analysis.md，記錄依據的逐字稿版本，
並執行與自動流程相同的程式檢查（lyanalysis.lint）。之後執行 `python run.py --only-site` 產生網站。
逐字稿之後若改版（例如公報刊出），自動流程仍會判定需要重新分析。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lyanalysis import collect, lint  # noqa: E402

TZ = ZoneInfo("Asia/Taipei")


def import_analysis(folder: Path, text: str, model: str) -> list[str]:
    """把分析寫入會議資料夾並記錄 meta.analysis；回傳程式檢查提示。"""
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"找不到會議資料：{folder.name}")
    text = text.strip()
    if not text.startswith("## "):
        raise ValueError("分析應以 H2（## ）段落開頭，與自動流程格式一致")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    issues = lint.check(text)
    (folder / "analysis.md").write_text(text + "\n", encoding="utf-8")
    meta["analysis"] = {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source": meta["source"], "source_rank": meta["source_rank"], "source_label": meta["source_label"],
        "transcript_sha256": meta["transcript_sha256"], "transcript_chars": meta["transcript_chars"],
        "lint": issues, "model": model, "mode": "manual", "usage": {},
    }
    meta.pop("analysis_error", None)
    collect.save_meta(folder, meta)
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug")
    parser.add_argument("analysis")
    parser.add_argument("--model", default="Claude（對話中撰寫，非 API）")
    args = parser.parse_args()
    try:
        issues = import_analysis(ROOT / "data" / "meetings" / args.slug,
                                 Path(args.analysis).read_text(encoding="utf-8"), args.model)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"已匯入 {args.slug}；程式檢查提示 {len(issues)} 項")
    for item in issues:
        print("  -", item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
