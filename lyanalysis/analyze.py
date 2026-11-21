"""呼叫 Claude API 產生客觀分析。

流程：逐字稿（過長時先逐段整理筆記）→ 撰寫完整分析 → 中立性審稿（可在設定關閉）。
會議資料放在可快取的區塊，撰寫與審稿兩次呼叫共用，節省費用。
模型的安全分類器婉拒時（stop_reason == "refusal"），改用 config 的 fallback_model 重跑。
"""
from __future__ import annotations

from pathlib import Path

from .laws import reference_text
from .transcript import chunk

ROOT = Path(__file__).resolve().parent.parent


class Refusal(RuntimeError):
    """模型的安全機制婉拒了這次請求。"""

    def __init__(self, model: str, category: str | None, explanation: str | None):
        self.model, self.category, self.explanation = model, category, explanation
        detail = "、".join(x for x in (category, explanation) if x) or "未提供原因"
        super().__init__(f"{model} 婉拒了請求（{detail}）")


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


def _call(client, model: str, system: str, material: str, instruction: str, max_tokens: int, usage: dict) -> str:
    """material 放在可快取區塊；instruction 每次不同。使用串流以支援長輸出。"""
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": material, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": instruction},
            ],
        }],
    ) as stream:
        message = stream.get_final_message()

    u = getattr(message, "usage", None)
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        usage[field] = usage.get(field, 0) + int(getattr(u, field, 0) or 0)
    usage["calls"] = usage.get("calls", 0) + 1

    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        get = (lambda k: details.get(k)) if isinstance(details, dict) else (lambda k: getattr(details, k, None))
        raise Refusal(model, get("category"), get("explanation"))
    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text").strip()
    if not text:
        raise RuntimeError(f"模型沒有回傳文字（stop_reason={message.stop_reason}）")
    if message.stop_reason == "max_tokens":
        text += "\n\n> ⚠ 本段因長度上限被截斷，內容可能不完整。"
    return text


def _run(meta: dict, transcript: str, cfg: dict, client, model: str, usage: dict) -> str:
    system = _read("prompts/system.md") + "\n\n# 法條參考\n\n" + reference_text()
    header = meeting_header(meta)
    parts = chunk(transcript, int(cfg["chunk_chars"]))

    if len(parts) == 1:
        material = f"{header}\n\n# 逐字稿\n\n{parts[0]}"
    else:
        notes = []
        for i, part in enumerate(parts, 1):
            prompt = _read("prompts/chunk.md").replace("{i}", str(i)).replace("{n}", str(len(parts)))
            note = _call(client, model, system, f"{header}\n\n# 逐字稿片段\n\n{part}", prompt,
                         int(cfg["chunk_max_tokens"]), usage)
            notes.append(f"## 第 {i}/{len(parts)} 段筆記\n\n{note}")
        material = f"{header}\n\n# 逐字稿分段筆記\n\n" + "\n\n".join(notes)

    draft = _call(client, model, system, material, _read("prompts/final.md"), int(cfg["final_max_tokens"]), usage)
    if cfg.get("review_pass"):
        draft = _call(client, model, system, material,
                      f"{_read('prompts/review.md')}\n\n# 待審草稿\n\n{draft}", int(cfg["final_max_tokens"]), usage)
    return draft


def analyze(meta: dict, transcript: str, cfg: dict, client) -> tuple[str, dict]:
    """回傳 (分析 Markdown, 紀錄)。紀錄含實際使用的模型、token 用量、是否改用備用模型。"""
    usage: dict = {}
    record: dict = {"model": cfg["model"], "usage": usage}
    try:
        text = _run(meta, transcript, cfg, client, cfg["model"], usage)
    except Refusal as first:
        fallback = cfg.get("fallback_model")
        if not fallback or fallback == cfg["model"]:
            raise
        record.update({"model": fallback, "refusal": str(first)})
        text = _run(meta, transcript, cfg, client, fallback, usage)
    return text, record
