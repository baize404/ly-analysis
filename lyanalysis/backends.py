"""AI 服務介面：同一套提示詞可以交給不同服務執行。

依 GitHub Secrets 中設定了哪個金鑰自動選用（順序見 config.json 的 provider_order）：

- claude_api           ANTHROPIC_API_KEY          Anthropic API，按用量付費
- claude_subscription  CLAUDE_CODE_OAUTH_TOKEN    Claude Pro／Max 訂閱，透過 Claude Code 的 `claude -p` 執行，
                                                   計入訂閱的用量上限，不另外收費
- gemini               GEMINI_API_KEY             Google Gemini API 免費額度（每日次數有限）

每個服務都實作 complete()：送出系統提示、會議資料與指示，回傳文字，並累計用量。
錯誤分三類，讓上層決定怎麼處理：
- Refusal：模型的安全機制婉拒 → 換備用模型
- ModelUnavailable：模型不存在或已下架 → 換備用模型
- QuotaExhausted：免費額度或訂閱用量用完 → 換備用模型；全部用完就停止本次分析，下次排程再試
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TRUNCATED_NOTE = "\n\n> ⚠ 本段因長度上限被截斷，內容可能不完整。"

SECRETS = {
    "claude_api": "ANTHROPIC_API_KEY",
    "claude_subscription": "CLAUDE_CODE_OAUTH_TOKEN",
    "gemini": "GEMINI_API_KEY",
}
DEFAULT_ORDER = ["claude_api", "claude_subscription", "gemini"]


class Refusal(RuntimeError):
    """模型的安全機制婉拒了這次請求。"""

    def __init__(self, model: str, category: str | None, explanation: str | None):
        self.model, self.category, self.explanation = model, category, explanation
        detail = "、".join(x for x in (category, explanation) if x) or "未提供原因"
        super().__init__(f"{model} 婉拒了請求（{detail}）")


class BackendError(RuntimeError):
    """服務回報的錯誤；explain 是給人看的原因與處理方式。"""

    def __init__(self, message: str, explain: str | None = None):
        super().__init__(message)
        self.explain = explain


class ModelUnavailable(BackendError):
    """指定的模型不存在、已下架，或此帳戶不能使用。"""


class QuotaExhausted(BackendError):
    """免費額度或訂閱用量已用完。"""


class NoBackend(RuntimeError):
    """沒有設定任何 AI 服務的金鑰。"""


class Backend:
    name = ""
    label = ""

    def complete(self, model: str, system: str, material: str, instruction: str,
                 max_tokens: int, usage: dict) -> str:  # pragma: no cover - 介面
        raise NotImplementedError


def _add(usage: dict, field: str, value) -> None:
    usage[field] = usage.get(field, 0) + int(value or 0)


# ---------------------------------------------------------------- Anthropic API

class ClaudeAPIBackend(Backend):
    name = "claude_api"
    label = "Anthropic API"

    def __init__(self, client=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(max_retries=4)
        self.client = client

    def complete(self, model, system, material, instruction, max_tokens, usage):
        """material 放在可快取區塊；instruction 每次不同。使用串流以支援長輸出。"""
        with self.client.messages.stream(
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
            _add(usage, field, getattr(u, field, 0))
        _add(usage, "calls", 1)

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            get = (lambda k: details.get(k)) if isinstance(details, dict) else (lambda k: getattr(details, k, None))
            raise Refusal(model, get("category"), get("explanation"))
        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text").strip()
        if not text:
            raise BackendError(f"模型沒有回傳文字（stop_reason={message.stop_reason}）")
        if message.stop_reason == "max_tokens":
            text += TRUNCATED_NOTE
        return text


# ------------------------------------------------------- Claude 訂閱（claude -p）

AUTH_HINT = ("Claude 訂閱權杖無效、過期或已被撤銷。請在自己的電腦上重新執行 `claude setup-token`，"
             "再到 repo 的 Settings → Secrets and variables → Actions 更新 CLAUDE_CODE_OAUTH_TOKEN。")
LIMIT_HINT = "Claude 訂閱的用量上限已用完。剩下的會議會在下次排程（用量重置後）自動分析。"


class ClaudeCodeBackend(Backend):
    """用 Claude Code 的非互動模式執行，認證來自 CLAUDE_CODE_OAUTH_TOKEN（訂閱權杖）。

    關閉所有工具，系統提示整個換成本站的寫作規則，會議資料從 stdin 送入。
    不使用 --bare，因為 bare 模式不讀取訂閱權杖。
    """
    name = "claude_subscription"
    label = "Claude 訂閱（Claude Code）"

    def __init__(self, command: str | None = None, runner=subprocess.run, timeout: int = 1800):
        self.command = command or shutil.which("claude")
        if not self.command:
            raise BackendError("找不到 claude 指令", explain=(
                "已設定 CLAUDE_CODE_OAUTH_TOKEN，但執行環境沒有安裝 Claude Code。"
                "請確認 .github/workflows/update.yml 裡的「安裝 Claude Code」步驟有執行。"))
        self.runner, self.timeout = runner, timeout

    def complete(self, model, system, material, instruction, max_tokens, usage):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_file = Path(tmp) / "system.md"
            prompt_file.write_text(system, encoding="utf-8")
            args = [self.command, "-p", "--model", model, "--system-prompt-file", str(prompt_file),
                    "--tools", "", "--output-format", "json", "--no-session-persistence"]
            env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens),
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
            try:
                proc = self.runner(args, input=f"{material}\n\n---\n\n{instruction}", capture_output=True,
                                   text=True, cwd=tmp, env=env, timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                raise BackendError("Claude Code 執行逾時", explain=(
                    f"Claude Code 超過 {self.timeout // 60} 分鐘沒有完成。通常是服務暫時忙碌，下次排程會重試。")) from exc

        data = _last_json(proc.stdout)
        message = (data or {}).get("result") or proc.stderr.strip() or proc.stdout.strip() or "（沒有輸出）"
        if data is None or data.get("is_error") or proc.returncode != 0:
            raise _classify_claude_code_error(model, str(message))

        u = data.get("usage") or {}
        for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            _add(usage, field, u.get(field))
        _add(usage, "calls", 1)
        if data.get("total_cost_usd") is not None:
            usage["estimated_cost_usd"] = round(usage.get("estimated_cost_usd", 0) + float(data["total_cost_usd"]), 4)

        text = str(data.get("result") or "").strip()
        if not text:
            raise BackendError(f"Claude Code 沒有回傳文字（subtype={data.get('subtype')}）")
        if data.get("stop_reason") == "max_tokens":
            text += TRUNCATED_NOTE
        return text


def _last_json(stdout: str) -> dict | None:
    """--output-format json 會輸出一個 JSON 物件；保險起見從最後一行往前找。"""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    return None


def _classify_claude_code_error(model: str, message: str) -> Exception:
    low = message.lower()
    if "usage policy" in low or "refus" in low:
        return Refusal(model, "usage_policy", message[:200])
    if any(k in low for k in ("usage limit", "limit reached", "rate_limit", "rate limit", "out of extra usage",
                              "hit your limit", "billing_error")):
        return QuotaExhausted(message[:300], explain=LIMIT_HINT)
    if any(k in low for k in ("authentication", "oauth", "invalid api key", "not logged in", "/login", "401",
                              "account_on_hold", "invalid token", "token expired", "expired token")):
        return BackendError(message[:300], explain=AUTH_HINT)
    if "model_not_found" in low or ("model" in low and "not found" in low) or "invalid model" in low:
        return ModelUnavailable(message[:300], explain=(
            f"Claude Code 找不到模型 {model}。請檢查 config.json 的 providers.claude_subscription 模型名稱。"))
    if "overloaded" in low or "529" in low or "server_error" in low or "500" in low:
        return BackendError(message[:300], explain="Anthropic 伺服器忙碌或暫時故障。下次排程會重試。")
    return BackendError(message[:300], explain=f"Claude Code 執行失敗：{message[:200]}")


# ------------------------------------------------------------ Google Gemini API

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}
GEMINI_KEY_HINT = ("Gemini API 金鑰無效或沒有權限。請到 Google AI Studio 重新建立金鑰，"
                   "再到 repo 的 Settings → Secrets and variables → Actions 更新 GEMINI_API_KEY。")
GEMINI_QUOTA_HINT = ("Gemini 免費額度今天已用完（每日次數在美國太平洋時間午夜重置，約臺灣時間下午 3 點或 4 點）。"
                     "剩下的會議會在下次排程自動分析。")


def _seconds(value) -> float | None:
    match = re.match(r"^\s*([\d.]+)\s*s?\s*$", str(value or ""))
    return float(match.group(1)) if match else None


class GeminiBackend(Backend):
    name = "gemini"
    label = "Google Gemini API 免費額度"

    def __init__(self, api_key: str, opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic,
                 min_interval: float = 30.0, thinking_buffer: int = 16000, max_retries: int = 5, max_wait: float = 180):
        self.api_key, self.opener, self.sleep, self.clock = api_key, opener, sleep, clock
        self.min_interval, self.thinking_buffer = min_interval, thinking_buffer
        self.max_retries, self.max_wait = max_retries, max_wait
        self._last_call: float | None = None

    def _pace(self) -> None:
        """免費額度每分鐘次數很少，兩次呼叫之間至少間隔 min_interval 秒。"""
        if self._last_call is not None:
            wait = self.min_interval - (self.clock() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = self.clock()

    def complete(self, model, system, material, instruction, max_tokens, usage):
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": material}, {"text": instruction}]}],
            # 思考過程也算在輸出上限內，所以多留一些空間
            "generationConfig": {"maxOutputTokens": min(65536, int(max_tokens) + self.thinking_buffer)},
        }
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = GEMINI_ENDPOINT.format(model=model)
        data = None
        for attempt in range(self.max_retries + 1):
            self._pace()
            request = urllib.request.Request(url, data=payload, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": self.api_key})
            try:
                with self.opener(request, timeout=900) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                retry_after = self._handle_http_error(model, exc)
                if attempt >= self.max_retries:
                    raise BackendError(f"Gemini 連續 {attempt + 1} 次失敗（HTTP {exc.code}）", explain=(
                        "Gemini 服務一直回報忙碌或每分鐘次數超過上限。下次排程會重試。")) from exc
                self.sleep(min(self.max_wait, retry_after))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise BackendError(f"連不上 Gemini API：{exc}", explain=(
                        "連不上 Gemini API（網路逾時或中斷）。通常是暫時性問題，下次排程會重試。")) from exc
                self.sleep(min(self.max_wait, 10 * 2 ** attempt))
        return self._parse(model, data or {}, usage)

    def _handle_http_error(self, model: str, exc: urllib.error.HTTPError) -> float:
        """不可重試的錯誤直接拋出；可重試的回傳建議等待秒數。"""
        try:
            info = json.loads(exc.read().decode("utf-8")).get("error", {})
        except Exception:
            info = {}
        message = str(info.get("message") or exc.reason or "")
        low = message.lower()
        details = info.get("details") or []
        if exc.code == 429:
            quota_ids = " ".join(str(v.get("quotaId", "")) for d in details for v in (d.get("violations") or []))
            if "perday" in quota_ids.lower() or "per day" in low or "daily" in low:
                raise QuotaExhausted(f"{model}：{message[:200]}", explain=GEMINI_QUOTA_HINT)
            delay = next((_seconds(d.get("retryDelay")) for d in details if d.get("retryDelay")), None)
            return delay if delay is not None else 60.0
        if exc.code == 404 or (exc.code == 400 and "not found" in low) or "is not supported" in low:
            raise ModelUnavailable(f"{model}：{message[:200]}", explain=(
                f"Gemini 找不到模型 {model}（可能已改名或下架），已改用備用模型。"
                "可在 config.json 的 providers.gemini 更新模型名稱。"))
        if exc.code in (401, 403) or "api key" in low or "permission" in low:
            raise BackendError(f"Gemini HTTP {exc.code}：{message[:200]}", explain=GEMINI_KEY_HINT)
        if exc.code in (500, 502, 503, 504):
            return 30.0
        raise BackendError(f"Gemini HTTP {exc.code}：{message[:200]}", explain=(
            f"Gemini 拒絕了請求（{message[:150]}）。可能是逐字稿過長或設定的輸出上限超過模型限制。"))

    def _parse(self, model: str, data: dict, usage: dict) -> str:
        meta = data.get("usageMetadata") or {}
        _add(usage, "input_tokens", meta.get("promptTokenCount"))
        _add(usage, "output_tokens", meta.get("candidatesTokenCount"))
        _add(usage, "thinking_tokens", meta.get("thoughtsTokenCount"))
        _add(usage, "cache_read_input_tokens", meta.get("cachedContentTokenCount"))
        _add(usage, "calls", 1)

        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise Refusal(model, str(blocked), None)
        candidate = (data.get("candidates") or [{}])[0]
        reason = candidate.get("finishReason")
        if reason in GEMINI_BLOCKED:
            raise Refusal(model, str(reason), None)
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if not text:
            raise BackendError(f"Gemini 沒有回傳文字（finishReason={reason}）")
        if reason == "MAX_TOKENS":
            text += TRUNCATED_NOTE
        return text


# ------------------------------------------------------------------ 選擇服務

def provider_config(cfg: dict, name: str) -> dict:
    """把 config.json 的共用設定與該服務的專屬設定合併。"""
    own = cfg.get("providers", {}).get(name) or {}
    merged = {**cfg, **own}
    if "fallback_models" in own:  # 服務自己的備用清單優先，不混入共用的 fallback_model
        merged.pop("fallback_model", None)
    merged["provider"] = name
    return merged


def select_backend(cfg: dict, env=None) -> tuple[Backend, dict]:
    """依設定順序，回傳第一個有設定金鑰的服務。LY_PROVIDER 可強制指定。"""
    env = os.environ if env is None else env
    forced = (env.get("LY_PROVIDER") or "").strip()
    order = [forced] if forced else list(cfg.get("provider_order") or DEFAULT_ORDER)
    for name in order:
        secret = SECRETS.get(name)
        if not secret:
            raise BackendError(f"未知的 AI 服務：{name}", explain=(
                f"config.json 的 provider_order 或 LY_PROVIDER 寫了不認得的服務「{name}」，"
                f"可用的有：{'、'.join(SECRETS)}。"))
        if not env.get(secret):
            continue
        pcfg = provider_config(cfg, name)
        if name == "claude_api":
            return ClaudeAPIBackend(), pcfg
        if name == "claude_subscription":
            return ClaudeCodeBackend(), pcfg
        return GeminiBackend(env[secret], min_interval=float(pcfg.get("min_interval_seconds", 30))), pcfg
    raise NoBackend("沒有設定任何 AI 服務的金鑰")
