"""把 LYAPI 的 AI 逐字稿或公報發言紀錄整理成「[時間] 講者：內容」格式。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

CJK = re.compile(r"[一-鿿]")
UNKNOWN = "（未辨識講者）"
ANON = re.compile(r"^SPEAKER_(\d+)$")
# AI 講者辨識的稱謂寫法，例如「莊委員瑞雄」「韓院長國瑜」「江副院長啟臣」。
TITLES = ["委員", "院長", "副院長", "召委", "召集委員"]


def build_roster(legislators: list[dict]) -> dict[str, tuple[str, str]]:
    """建立「名稱寫法 → (姓名, 標示)」對照。

    AI 講者辨識會把委員寫成「莊委員瑞雄」，公報則寫「莊瑞雄」，都要對得上。
    立法院長、副院長也是立委，以「韓院長國瑜」這類寫法出現時，標示中保留職稱。
    """
    roster: dict[str, tuple[str, str]] = {}
    for lg in legislators:
        name = (lg.get("委員姓名") or "").strip()
        if not name:
            continue
        party = (lg.get("黨籍") or "").strip()
        caucus = (lg.get("黨團") or "").strip()
        label = party if (not caucus or caucus == party) else f"{party}／{caucus}黨團"
        cjk = "".join(CJK.findall(name)) or name
        for alias in (name, cjk):
            roster.setdefault(alias, (name, label))
        splits = [1, 2] if len(cjk) >= 4 else [1]  # 四字以上可能是複姓
        for cut in splits:
            for title in TITLES:
                shown = label if title == "委員" else f"{title}，{label}" if label else title
                roster.setdefault(cjk[:cut] + title + cjk[cut:], (name, shown))
    return roster


def label_speaker(raw: str | None, roster: dict[str, tuple[str, str]]) -> str:
    raw = (raw or "").strip()
    if not raw:
        return UNKNOWN
    anon = ANON.match(raw)
    if anon:
        return f"（未辨識講者 {int(anon.group(1)) + 1}）"
    hit = roster.get(raw)
    if hit:
        name, label = hit
        return f"{name}（{label}）" if label else name
    return raw


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _offset(seconds: float) -> str:
    seconds = int(seconds)
    return f"+{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def ivod_lines(ivod: dict, roster: dict, merge_gap: float = 4.0, max_para: int = 300) -> list[dict]:
    """把 whisperx 文字段落配上 pyannote 講者，換算成會議當天的時鐘時間。"""
    tr = ivod.get("transcript") or {}
    # 原始資料偶有順序顛倒，依開始秒數排序
    segments = sorted(tr.get("whisperx") or [], key=lambda s: float(s.get("start") or 0))
    diar = sorted(tr.get("pyannote") or [], key=lambda d: float(d.get("start") or 0))
    base = _parse_dt(ivod.get("開始時間"))
    lines: list[dict] = []
    j = 0
    last_speaker = UNKNOWN
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start, end = float(seg.get("start") or 0), float(seg.get("end") or 0)
        while j < len(diar) and float(diar[j].get("end") or 0) < start - 30:
            j += 1
        best, best_overlap = None, 0.0
        k = j
        while k < len(diar) and float(diar[k].get("start") or 0) <= end:
            d = diar[k]
            overlap = min(end, float(d.get("end") or 0)) - max(start, float(d.get("start") or 0))
            if overlap > best_overlap:
                best, best_overlap = d, overlap
            k += 1
        speaker = label_speaker(best.get("speaker_name") or best.get("speaker"), roster) if best else last_speaker
        clock = (base + timedelta(seconds=start)).strftime("%H:%M:%S") if base else _offset(start)
        prev = lines[-1] if lines else None
        if prev and prev["speaker"] == speaker and start - prev["_end"] <= merge_gap and len(prev["text"]) < max_para:
            prev["text"] += " " + text
            prev["_end"] = end
        else:
            lines.append({"time": clock, "speaker": speaker, "text": text, "_end": end})
        last_speaker = speaker
    for line in lines:
        line.pop("_end", None)
    return lines


def speech_lines(speeches: list[dict], roster: dict) -> list[dict]:
    """公報發言紀錄（官方逐字紀錄）。"""
    out: list[dict] = []
    for sp in sorted(speeches, key=lambda s: int(s.get("順序") or 0)):
        text = (sp.get("內文") or "").strip()
        if not text:
            continue
        name = (sp.get("發言者") or sp.get("發言者原文") or "").strip()
        role = (sp.get("發言者職稱") or sp.get("發言者身分") or "").strip()
        if name in roster and role != "主席":
            speaker = label_speaker(name, roster)
        elif role and role != name:
            speaker = f"{name}（{role}）" if name else role
        else:
            speaker = name or UNKNOWN
        when = _parse_dt(sp.get("發言時間"))
        out.append({"time": when.strftime("%H:%M") if when else "", "speaker": speaker, "text": text})
    return out


def render(lines: list[dict]) -> str:
    rows = []
    for line in lines:
        prefix = f"[{line['time']}] " if line.get("time") else ""
        rows.append(f"{prefix}{line['speaker']}：{line['text']}")
    return "\n".join(rows)


def chunk(text: str, max_chars: int) -> list[str]:
    """依行切段，每段不超過 max_chars（單行過長時獨立成段）。"""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if current and size + len(line) + 1 > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
