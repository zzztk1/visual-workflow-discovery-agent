from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_STEPFUN_BASE = "https://api.stepfun.com/v1"
DEFAULT_STEPFUN_MODEL = "step-3.5-flash"


def load_env_files(project_root: Path) -> List[str]:
    """Load StepFun env vars from this project or sibling projects.

    Existing process env wins. Values are never printed by callers; the returned
    list only contains file paths that contributed at least one variable.
    """

    candidates = [
        project_root / ".env",
        project_root.parent / "media-agent" / ".env",
        project_root.parent / "ai-customer-service" / ".env",
    ]
    loaded: List[str] = []
    for path in candidates:
        if not path.exists():
            continue
        touched = False
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key.startswith("STEPFUN_") and value and not os.getenv(key):
                os.environ[key] = value
                touched = True
        if touched:
            loaded.append(str(path))
    return loaded


def salvage_json(text: str) -> Any:
    text = text.strip()
    if not text:
        raise ValueError("empty LLM response")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start_candidates = [idx for idx in [text.find("{"), text.find("[")] if idx >= 0]
        if not start_candidates:
            raise
        start = min(start_candidates)
        end = max(text.rfind("}"), text.rfind("]"))
        if end <= start:
            raise
        return json.loads(text[start : end + 1])


def _usage_to_dict(usage: Any) -> Dict[str, int]:
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}
    if hasattr(usage, "model_dump"):
        raw = usage.model_dump()
    elif isinstance(usage, dict):
        raw = usage
    else:
        raw = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
            "cached_tokens": getattr(usage, "cached_tokens", 0),
        }
    prompt_details = raw.get("prompt_tokens_details") or {}
    cached = raw.get("cached_tokens") or prompt_details.get("cached_tokens") or 0
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    total = int(raw.get("total_tokens") or (prompt + completion))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": int(cached or 0),
    }


@dataclass
class LLMResult:
    data: Any
    text: str
    usage: Dict[str, Any]
    fallback_used: bool = False
    error: str = ""


class StepFunLLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        model: str,
        enabled: bool,
        price_per_1k_usd: float,
        timeout_sec: float,
        env_files_loaded: Optional[List[str]] = None,
    ) -> None:
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.enabled = enabled
        self.price_per_1k_usd = price_per_1k_usd
        self.timeout_sec = timeout_sec
        self.env_files_loaded = env_files_loaded or []
        self._client = None

    @classmethod
    def from_env(cls, project_root: Path, config: Dict[str, Any]) -> "StepFunLLMClient":
        loaded = load_env_files(project_root)
        enabled = bool(config.get("llm_enabled", True))
        return cls(
            api_key=os.getenv("STEPFUN_API_KEY", "").strip(),
            api_base=os.getenv("STEPFUN_API_BASE", DEFAULT_STEPFUN_BASE).strip() or DEFAULT_STEPFUN_BASE,
            model=os.getenv("STEPFUN_MODEL", DEFAULT_STEPFUN_MODEL).strip() or DEFAULT_STEPFUN_MODEL,
            enabled=enabled,
            price_per_1k_usd=float(config.get("llm_price_per_1k_usd", 0.0006)),
            timeout_sec=float(config.get("llm_timeout_sec", 45)),
            env_files_loaded=loaded,
        )

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key and self.api_base and self.model)

    def safe_config(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "provider": "stepfun",
            "api_base": self.api_base,
            "model": self.model,
            "env_files_loaded_count": len(self.env_files_loaded),
            "has_api_key": bool(self.api_key),
        }

    def complete_json(
        self,
        *,
        node: str,
        system: str,
        payload: Dict[str, Any],
        fallback: Any,
        temperature: float = 0.2,
    ) -> LLMResult:
        if not self.configured:
            reason = "llm_disabled" if not self.enabled else "missing_stepfun_api_key"
            return LLMResult(
                data=fallback,
                text="",
                usage=self._usage(node=node, latency_ms=0, error=reason, token_source="none"),
                fallback_used=True,
                error=reason,
            )

        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover
            return LLMResult(
                data=fallback,
                text="",
                usage=self._usage(node=node, latency_ms=0, error=f"openai_import_failed:{exc}", token_source="none"),
                fallback_used=True,
                error=f"openai_import_failed:{exc}",
            )

        if self._client is None:
            self._client = OpenAI(api_key=self.api_key, base_url=self.api_base, timeout=self.timeout_sec)

        user_content = json.dumps(payload, ensure_ascii=False, indent=2)
        prompt = (
            "Return valid JSON only. Do not include markdown fences. "
            "Use Chinese for user-facing fields.\n\n"
            + user_content
        )
        start = time.perf_counter()
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                extra_body={"thinking": {"type": "disabled"}},
            )
            latency_ms = (time.perf_counter() - start) * 1000
            text = response.choices[0].message.content or ""
            data = salvage_json(text)
            usage = self._usage(
                node=node,
                latency_ms=latency_ms,
                usage=_usage_to_dict(getattr(response, "usage", None)),
                error="",
                token_source="provider_usage",
            )
            return LLMResult(data=data, text=text, usage=usage, fallback_used=False)
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return LLMResult(
                data=fallback,
                text="",
                usage=self._usage(
                    node=node,
                    latency_ms=latency_ms,
                    error=f"{type(exc).__name__}: {exc}",
                    token_source="provider_error",
                ),
                fallback_used=True,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _usage(
        self,
        *,
        node: str,
        latency_ms: float,
        error: str = "",
        usage: Optional[Dict[str, int]] = None,
        token_source: str,
    ) -> Dict[str, Any]:
        usage = usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}
        total = int(usage.get("total_tokens", 0))
        return {
            "node": node,
            "provider": "stepfun",
            "model": self.model,
            "api_base": self.api_base,
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "total_tokens": total,
            "cached_tokens": int(usage.get("cached_tokens", 0)),
            "estimated_cost_usd": round((total / 1000.0) * self.price_per_1k_usd, 6),
            "latency_ms": round(latency_ms, 2),
            "token_source": token_source,
            "error": error,
        }
