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

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent


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


class BaseProvider:
    name = "base"

    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def available(self) -> bool:
        raise NotImplementedError

    def complete(self, prompt: str, *, temperature: float = 0.0,
                 timeout: int = 45) -> LLMResponse:
        raise NotImplementedError


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
                 timeout: int = 45) -> LLMResponse:
        return LLMResponse(
            text="", ok=False, provider=self.name, model=self.model,
            error="未設定 LLM_PROVIDER 或對應的 API 金鑰；已降級為僅規則層模式。",
        )


class GeminiProvider(BaseProvider):
    name = "gemini"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model or os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, prompt: str, *, temperature: float = 0.0,
                 timeout: int = 45) -> LLMResponse:
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
                        # 要求結構化輸出，減少「模型多講一句話害 JSON 解析失敗」
                        "responseMimeType": "application/json",
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

    def complete(self, prompt: str, *, temperature: float = 0.0,
                 timeout: int = 45) -> LLMResponse:
        t0 = time.time()
        try:
            r = requests.post(
                self.ENDPOINT,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "temperature": temperature,
                      "response_format": {"type": "json_object"},
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

    def complete(self, prompt: str, *, temperature: float = 0.0,
                 timeout: int = 45) -> LLMResponse:
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
