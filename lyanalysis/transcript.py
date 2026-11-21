"""把 LYAPI 的 AI 逐字稿或公報發言紀錄整理成「[時間] 講者：內容」格式。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

CJK = re.compile(r"[一-鿿]")
UNKNOWN = "（未辨識講者）"


def build_roster(legislators: list[dict]) -> dict[str, tuple[str, str]]:
    """建立「名稱寫法 → (姓名, 黨籍標示)」對照。

    AI 講者辨識會把委員寫成「莊委員瑞雄」，公報則寫「莊瑞雄」，兩種都要對得上。
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
        aliases = {name, cjk, cjk[0] + "委員" + cjk[1:]}
        if len(cjk) >= 4:  # 複姓
            aliases.add(cjk[:2] + "委員" + cjk[2:])
        for alias in aliases:
            roster.setdefault(alias, (name, label))
    return roster


def label_speaker(raw: str | None, roster: dict[str, tuple[str, str]]) -> str:
    raw = (raw or "").strip()
    if not raw:
        return UNKNOWN
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
    segments = tr.get("whisperx") or []
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
