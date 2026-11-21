"""LYAPI v2 用戶端（https://v2.ly.govapi.tw）。

資料由 OpenFun 彙整自立法院各資料庫，以 CC-BY-4.0 授權。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://v2.ly.govapi.tw"
UA = "ly-analysis/1.0 (+https://github.com/baize404/ly-analysis)"


class LYAPIError(RuntimeError):
    pass


def _get(path: str, params: dict | None = None, retries: int = 3, pause: float = 0.4) -> dict:
    url = BASE + urllib.parse.quote(path, safe="/-_")
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            time.sleep(pause)  # 對公益服務保持禮貌的請求頻率
            return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise LYAPIError(f"GET {url} 失敗：{last}")


def _paged(path: str, key: str, params: dict, limit: int = 100, max_pages: int = 50) -> list[dict]:
    items: list[dict] = []
    page = 1
    while page <= max_pages:
        data = _get(path, {**params, "limit": limit, "page": page})
        batch = data.get(key) or []
        items.extend(batch)
        if not batch or page >= int(data.get("total_page") or 1):
            break
        page += 1
    return items


def list_ivods(date: str) -> list[dict]:
    """某一天的所有 IVOD 影片（完整會議與委員發言片段）。"""
    return _paged("/ivods", "ivods", {"日期": date})


def get_ivod(ivod_id: int | str) -> dict:
    """單一影片，含 AI 逐字稿（transcript.whisperx / transcript.pyannote）。"""
    return _get(f"/ivod/{ivod_id}").get("data") or {}


def get_meet(code: str) -> dict:
    return _get(f"/meet/{code}").get("data") or {}


def list_speeches(code: str, date: str) -> list[dict]:
    """立法院公報的發言紀錄（官方逐字紀錄，通常會議後數週才刊出）。"""
    return _paged(f"/meet/{code}/speeches", "speeches", {"日期": date}, limit=200, max_pages=100)


def count_speeches(code: str, date: str) -> int:
    data = _get(f"/meet/{code}/speeches", {"日期": date, "limit": 1})
    return int(data.get("total") or 0)


def list_legislators(term: int = 11) -> list[dict]:
    return _paged("/legislators", "legislators", {"屆": term}, limit=200)
