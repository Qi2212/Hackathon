"""可插拔的 LLM 任务理解器。

约定：
- 默认 `LLM_PROVIDER=none`，完全走确定性解析器，不依赖网络；
- 配置 `LLM_PROVIDER=openai_compatible` 后调用任意 OpenAI 兼容的
  `/chat/completions`，只负责“理解与补全”，不得虚构资源；
- 任何网络/解析失败都返回 `LLMResult(data=None, error=...)`，由调用方回退，
  绝不让 LLM 故障影响 analyze 接口的可用性。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0

SYSTEM_PROMPT = """你是校园 GPU 算力平台的任务解析器。
只提取用户原文中明确出现、或可以安全推断的信息；不要虚构 GPU 型号、数量、显存、价格或资源可用性。
必须输出严格 JSON（不要 markdown 代码块），字段如下，没有信息就填 null：
{
  "task_type": "training|fine_tuning|inference|data_processing|simulation|notebook|unknown",
  "gpu_count": int|null,
  "gpu_models": [string]|null,
  "min_gpu_memory_mib": int|null,
  "cpu_millicores": int|null,
  "memory_mib": int|null,
  "ephemeral_storage_mib": int|null,
  "duration_seconds": int|null,
  "deadline": "ISO8601"|null,
  "budget_max": number|null,
  "currency": "CNY|USD"|null,
  "interruptible": bool|null,
  "checkpointable": bool|null,
  "urgent": bool|null,
  "confidence": 0.0-1.0
}
gpu_models 只能从给定的可用型号中选择；未给出候选时该字段必须为 null。"""


@dataclass
class LLMResult:
    data: dict[str, Any] | None
    provider: str
    error: str | None = None

    @property
    def used(self) -> bool:
        return self.data is not None


class LLMProvider(Protocol):
    name: str

    def suggest(self, text: str, known_models: list[str]) -> LLMResult: ...


class NoopProvider:
    """未配置 LLM 时的确定性回退。"""

    name = "none"

    def suggest(self, text: str, known_models: list[str]) -> LLMResult:
        return LLMResult(None, self.name, None)


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def suggest(self, text: str, known_models: list[str]) -> LLMResult:
        models_hint = "、".join(known_models) if known_models else "（未提供，gpu_models 必须为 null）"
        user_prompt = f"可用 GPU 型号：{models_hint}\n用户原文：{text}"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            response = self._client.post("/chat/completions", json=body, headers=headers)
            if response.status_code >= 400:
                return LLMResult(None, self.name, f"http_{response.status_code}: {response.text[:200]}")
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                return LLMResult(None, self.name, "empty completion")
            return LLMResult(_loads_json_object(content), self.name, None)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            return LLMResult(None, self.name, f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        self._client.close()


def _loads_json_object(content: str) -> dict[str, Any] | None:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def provider_from_env() -> LLMProvider:
    kind = os.getenv("LLM_PROVIDER", "none").strip().lower()
    if kind not in {"openai", "openai-compatible", "openai_compatible"}:
        return NoopProvider()
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    if not base_url or not model:
        return NoopProvider()
    return OpenAICompatibleProvider(
        base_url=base_url,
        api_key=os.getenv("LLM_API_KEY") or None,
        model=model,
        timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
    )
