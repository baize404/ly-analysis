"""用設定的 AI 服務產生客觀分析（服務細節見 backends.py）。

流程：逐字稿（過長時先逐段整理筆記）→ 撰寫完整分析 → 中立性審稿（可在設定關閉）。
主要模型婉拒、下架或額度用完時，依序改用備用模型重跑。
"""
from __future__ import annotations

from pathlib import Path

from .backends import Backend, ClaudeAPIBackend, ModelUnavailable, QuotaExhausted, Refusal
from .laws import reference_text
from .transcript import chunk

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def meeting_header(meta: dict) -> str:
    rows = [
        "# 會議資訊",
        f"- 會議：{meta.get('title')}（{meta.get('code')}）",
        f"- 種類：{meta.get('kind')}" + (f"（{'、'.join(meta['committees'])}）" if meta.get("committees") else ""),
        f"- 日期：{meta.get('date')}",
    ]
    if meta.get("name"):
        rows.append(f"- 影片標示的會議名稱與事由：{meta['name']}")
    if meta.get("reason"):
        rows.append(f"- 議事日程所列事由：{meta['reason']}")
    if meta.get("agenda"):
        rows.append("- 議事日程討論事項：")
        rows += [f"  - {item}" for item in meta["agenda"]]
    if meta.get("third_reading"):
        rows.append(f"- LYAPI 記錄的三讀法案：{'、'.join(map(str, meta['third_reading']))}")
    rows.append(f"- 文字資料來源：{meta.get('source_label')}")
    if meta.get("source") in ("ai_full", "ai_clips"):
        rows.append("- 注意：AI 逐字稿可能有同音錯字，講者標示由自動辨識產生，可能錯置。")
    return "\n".join(rows)


def _run(meta: dict, transcript: str, cfg: dict, backend: Backend, model: str, usage: dict) -> str:
    system = _read("prompts/system.md") + "\n\n# 法條參考\n\n" + reference_text()
    header = meeting_header(meta)
    parts = chunk(transcript, int(cfg["chunk_chars"]))

    if len(parts) == 1:
        material = f"{header}\n\n# 逐字稿\n\n{parts[0]}"
    else:
        notes = []
        for i, part in enumerate(parts, 1):
            prompt = _read("prompts/chunk.md").replace("{i}", str(i)).replace("{n}", str(len(parts)))
            note = backend.complete(model, system, f"{header}\n\n# 逐字稿片段\n\n{part}", prompt,
                         int(cfg["chunk_max_tokens"]), usage)
            notes.append(f"## 第 {i}/{len(parts)} 段筆記\n\n{note}")
        material = f"{header}\n\n# 逐字稿分段筆記\n\n" + "\n\n".join(notes)

    draft = backend.complete(model, system, material, _read("prompts/final.md"), int(cfg["final_max_tokens"]), usage)
    if cfg.get("review_pass"):
        draft = backend.complete(model, system, material,
                      f"{_read('prompts/review.md')}\n\n# 待審草稿\n\n{draft}", int(cfg["final_max_tokens"]), usage)
    return draft


def models_for(cfg: dict) -> list[str]:
    """主要模型加上備用模型（fallback_models 清單，或舊設定的 fallback_model），去除重複。"""
    chain = [cfg["model"]] + list(cfg.get("fallback_models") or [])
    if cfg.get("fallback_model"):
        chain.append(cfg["fallback_model"])
    return list(dict.fromkeys(m for m in chain if m))


def analyze(meta: dict, transcript: str, cfg: dict, backend) -> tuple[str, dict]:
    """回傳 (分析 Markdown, 紀錄)。紀錄含實際使用的服務與模型、用量，以及跳過的模型與原因。

    backend 可以是 Backend，或（相容舊用法）Anthropic client。
    """
    if not isinstance(backend, Backend):
        backend = ClaudeAPIBackend(backend)
    usage: dict = {}
    record: dict = {"provider": backend.name, "provider_label": backend.label, "usage": usage}
    skipped: list[str] = []
    last: Exception | None = None
    quota: QuotaExhausted | None = None
    for model in models_for(cfg):
        try:
            text = _run(meta, transcript, cfg, backend, model, usage)
        except (Refusal, ModelUnavailable, QuotaExhausted) as exc:
            last = exc
            if isinstance(exc, QuotaExhausted):
                quota = exc
            skipped.append(f"{model}：{exc}")
            continue
        record["model"] = model
        if skipped:
            record["skipped_models"] = skipped
            refusals = [s for s in skipped if "婉拒" in s]
            if refusals:
                record["refusal"] = refusals[0]
        return text, record
    # 全部模型都無法完成：只要有一個是額度用完，就當作額度問題（下次排程再試）
    if quota is not None and last is not quota:
        raise QuotaExhausted("；".join(skipped)[:500], explain=quota.explain)
    raise last
