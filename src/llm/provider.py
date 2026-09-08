# -*- coding: utf-8 -*-
"""
LLM Provider 抽象層。

為什麼要抽象？三個實務理由，不是為了炫技：

1. 內部工具不能被單一廠商綁死。
   公司採購哪家 LLM 服務是 IT/資安決定的，不是工具開發者決定的。
   工具必須能在 Gemini / OpenAI / Anthropic / 地端模型之間切換，
   而且切換時只改設定檔，不動業務邏輯。

2. 必須能在「沒有金鑰」的環境下降級執行。
   同仁第一次拿到工具時多半還沒申請到金鑰。如果工具在這種情況下直接
   崩潰，導入就死在第一步。因此 NullProvider 是必要設計，不是替代方案。

3. 呼叫要能被稽核。
   每一次呼叫的 prompt、回應、耗時、是否失敗，都要留下紀錄，
   否則工具出錯時無法追查，同仁也不會信任它。

金鑰一律只從環境變數 / .env 讀取，絕不寫進程式碼或設定檔。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT / ".cache" / "llm"

# ---------------------------------------------------------------------------
# 配額保護：節流、退避重試、結果快取
# ---------------------------------------------------------------------------
# 這三件事不是為了「繞過」服務商的限制，而是任何要上線的 LLM 應用都必須有的：
#
#   節流   免費層與企業合約都有 RPM 上限。沒有節流，一次批次處理就會把
#          整批請求打成 429，而且失敗的那些看起來會像「模型解析不出來」，
#          導致錯誤的技術結論。（本專案第一次跑對照實驗就踩到這個坑：
#          LLM 欄位大面積掛零，實際原因是 429 而非解析品質。）
#
#   重試   429 與 5xx 是暫時性錯誤，應該退避後重試，而不是當成解析失敗。
#          服務商回傳的 retryDelay 要被尊重，不要自己亂猜。
#
#   快取   開發與評估階段會對同一批資料反覆執行。沒有快取，每跑一次實驗
#          就燒掉一次配額，而且結果還會因為模型的隨機性而不一致。
#          快取以 provider+模型+prompt 的雜湊為鍵，確保 prompt 一改就失效。
#
# 三者都可用環境變數關閉，因為在生產環境「永遠回傳快取」可能不是想要的行為。
_MIN_INTERVAL_S = float(os.getenv("LLM_MIN_INTERVAL_S", "4.0"))
_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
_USE_CACHE = os.getenv("LLM_CACHE", "1") != "0"

_throttle_lock = threading.Lock()
_last_call_at = 0.0


def _throttle() -> None:
    """確保兩次呼叫之間至少間隔 _MIN_INTERVAL_S 秒。"""
    global _last_call_at
    with _throttle_lock:
        wait = _MIN_INTERVAL_S - (time.time() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.time()


def _cache_key(provider: str, model: str, prompt: str, temperature: float,
               json_mode: bool) -> str:
    raw = f"{provider}|{model}|{temperature}|{json_mode}|{prompt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cache_get(key: str) -> str | None:
    if not _USE_CACHE:
        return None
    p = CACHE_DIR / f"{key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))["text"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _cache_put(key: str, text: str) -> None:
    if not _USE_CACHE:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.json").write_text(
            json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 快取失敗不該讓主流程掛掉


def _retry_delay_from(body: str, attempt: int) -> float:
    """
    優先採用服務商在錯誤內容裡指定的 retryDelay，取不到才用指數退避。

    自己亂猜等待時間是常見的錯誤：等太短會繼續打 429，
    等太長則讓批次處理慢到沒人願意用。
    """
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body)
    if m:
        return float(m.group(1)) + 1.0
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", body)
    if m:
        return float(m.group(1)) + 1.0
    return min(2.0 ** attempt, 30.0)


def _load_dotenv() -> None:
    """輕量 .env 載入，避免多一個硬相依。已存在的環境變數優先。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


@dataclass
class LLMResponse:
    text: str
    ok: bool
    provider: str
    model: str
    latency_ms: int = 0
    error: str = ""
    raw: dict = field(default_factory=dict)


def _is_retryable(error: str) -> bool:
    """
    區分「暫時性失敗」與「真正的失敗」。

    這個區分很重要：429 與 5xx 是服務端狀況，重試就會過；
    400（prompt 有問題）或 404（模型名稱錯）重試一百次也不會過，
    而且會白白消耗配額與時間。

    更重要的是，把暫時性失敗誤當成「模型解析不出來」，會導致
    完全錯誤的技術結論 —— 本專案第一次跑對照實驗時就發生過。
    """
    e = error or ""
    return ("HTTP 429" in e or "HTTP 500" in e or "HTTP 502" in e
            or "HTTP 503" in e or "HTTP 504" in e
            or "Timeout" in e or "ConnectionError" in e)


class BaseProvider:
    name = "base"

    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def available(self) -> bool:
        raise NotImplementedError

    def _raw_complete(self, prompt: str, *, temperature: float = 0.0,
                      timeout: int = 45, json_mode: bool = True) -> LLMResponse:
        """各家 provider 實作真正的 HTTP 呼叫。"""
        raise NotImplementedError

    def complete(self, prompt: str, *, temperature: float = 0.0,
                 timeout: int = 45, json_mode: bool = True) -> LLMResponse:
        """
        對外的統一入口：快取 → 節流 → 呼叫 → 退避重試。

        刻意寫在基底類別，讓三家 provider 共用同一套配額保護行為。
        新增一家 provider 時只要實作 _raw_complete，不必再處理這些。

        `json_mode` 決定是否要求模型輸出結構化 JSON。
        這個開關是必要的，不能全域寫死 —— 本工具有兩種完全不同的用途：

          解析信件  需要 JSON，欄位要能直接進資料表    -> json_mode=True
          撰寫草稿  需要自然語言，是要給人讀的信       -> json_mode=False

        早期版本為了讓解析穩定，在 provider 裡全域強制 JSON 輸出，
        結果回信草稿也被綁住，吐出一坨 {"subject":..., "content":...}
        直接顯示在畫面上給生管看。抽象層若不區分用途，就會這樣傷到使用者。
        """
        key = _cache_key(self.name, self.model, prompt, temperature, json_mode)
        cached = _cache_get(key)
        if cached is not None:
            return LLMResponse(cached, True, self.name, self.model,
                               0, "", {"cached": True})

        last: LLMResponse | None = None
        for attempt in range(_MAX_RETRIES + 1):
            _throttle()
            resp = self._raw_complete(prompt, temperature=temperature,
                                      timeout=timeout, json_mode=json_mode)
            if resp.ok:
                _cache_put(key, resp.text)
                return resp
            last = resp
            if attempt == _MAX_RETRIES or not _is_retryable(resp.error):
                break
            time.sleep(_retry_delay_from(resp.error, attempt))
        return last or LLMResponse("", False, self.name, self.model, 0, "unknown error")


class NullProvider(BaseProvider):
    """
    無金鑰時的降級實作。

    刻意「明確地什麼都不做」，而不是偷偷回傳假資料 ——
    因為工具若在無 LLM 的情況下回傳看起來像真的結果，
    使用者無法分辨哪些欄位可信，這比直接失敗更危險。
    上層收到 ok=False 後，會退回只用規則層的結果，並在 UI 標示清楚。
    """
    name = "none"

    def __init__(self) -> None:
        super().__init__(model="(未設定)")

    @property
    def available(self) -> bool:
        return False

    def complete(self, prompt: str, *, temperature: float = 0.0,
                 timeout: int = 45, json_mode: bool = True) -> LLMResponse:
        # 覆寫而非實作 _raw_complete：無金鑰時沒有東西需要快取或重試。
        return LLMResponse(
            text="", ok=False, provider=self.name, model=self.model,
            error="未設定 LLM_PROVIDER 或對應的 API 金鑰；已降級為僅規則層模式。",
        )


class GeminiProvider(BaseProvider):
    name = "gemini"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _raw_complete(self, prompt: str, *, temperature: float = 0.0,
                      timeout: int = 45, json_mode: bool = True) -> LLMResponse:
        t0 = time.time()
        try:
            r = requests.post(
                self.ENDPOINT.format(model=self.model),
                # 金鑰放 header 而非 query string，避免出現在日誌或代理伺服器紀錄裡
                headers={"x-goog-api-key": self.api_key,
                         "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": temperature,
                        # 只在解析用途要求結構化輸出，減少「模型多講一句話
                        # 害 JSON 解析失敗」；寫草稿時必須關掉，否則會吐 JSON。
                        **({"responseMimeType": "application/json"} if json_mode else {}),
                    },
                },
                timeout=timeout,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code != 200:
                return LLMResponse("", False, self.name, self.model, ms,
                                   f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return LLMResponse(text, True, self.name, self.model, ms, raw=data)
        except Exception as e:  # 網路/格式問題一律降級，不讓工具整個掛掉
            return LLMResponse("", False, self.name, self.model,
                               int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}")


class OpenAIProvider(BaseProvider):
    name = "openai"
    ENDPOINT = "https://api.openai.com/v1/chat/completions"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model or os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _raw_complete(self, prompt: str, *, temperature: float = 0.0,
                      timeout: int = 45, json_mode: bool = True) -> LLMResponse:
        t0 = time.time()
        try:
            r = requests.post(
                self.ENDPOINT,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "temperature": temperature,
                      **({"response_format": {"type": "json_object"}} if json_mode else {}),
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code != 200:
                return LLMResponse("", False, self.name, self.model, ms,
                                   f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            return LLMResponse(data["choices"][0]["message"]["content"], True,
                               self.name, self.model, ms, raw=data)
        except Exception as e:
            return LLMResponse("", False, self.name, self.model,
                               int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}")


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    ENDPOINT = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5"))
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _raw_complete(self, prompt: str, *, temperature: float = 0.0,
                      timeout: int = 45, json_mode: bool = True) -> LLMResponse:
        t0 = time.time()
        try:
            r = requests.post(
                self.ENDPOINT,
                headers={"x-api-key": self.api_key,
                         "anthropic-version": "2023-06-01",
                         "Content-Type": "application/json"},
                json={"model": self.model, "max_tokens": 2048,
                      "temperature": temperature,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code != 200:
                return LLMResponse("", False, self.name, self.model, ms,
                                   f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            return LLMResponse(data["content"][0]["text"], True,
                               self.name, self.model, ms, raw=data)
        except Exception as e:
            return LLMResponse("", False, self.name, self.model,
                               int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}")


_REGISTRY = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}


def get_provider(name: str | None = None) -> BaseProvider:
    """
    依環境變數建立 provider；金鑰缺漏時自動降級為 NullProvider。

    降級是「安靜且明確」的：不丟例外（工具照跑），但 available=False，
    上層必須據此在 UI 上標示「本次結果未經 LLM 解析」。
    """
    _load_dotenv()
    name = (name or os.getenv("LLM_PROVIDER", "none")).strip().lower()
    cls = _REGISTRY.get(name)
    if cls is None:
        return NullProvider()
    p = cls()
    return p if p.available else NullProvider()


def extract_json(text: str) -> dict | list | None:
    """
    從模型回應中取出 JSON。

    即使要求了 JSON 輸出，模型偶爾仍會包上 ```json 圍籬或加一句前言。
    這個函式做三段防禦，避免因為格式小問題就整批解析失敗。
    """
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None
