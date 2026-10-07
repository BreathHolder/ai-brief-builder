"""LLM access with per-task model routing, provider fallback, JSON output and cost tracking."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from briefing.config import AppConfig, ModelRoute

log = logging.getLogger("briefing")


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_json(text: str) -> Any:
    """Parse a JSON object/array from model output, tolerating fences and stray prose."""
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON found in model output")
    start = min(starts)
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    return json.loads(cleaned[start : end + 1])


def usage_cost(usage: list[dict], prices: dict[str, tuple[float, float]]) -> tuple[float, list[str]]:
    """Total USD for recorded usage, plus the models that had no price configured."""
    total, unpriced = 0.0, set()
    for u in usage:
        price = prices.get(u["model"])
        if not price:
            unpriced.add(u["model"])
            continue
        total += u["input_tokens"] / 1e6 * price[0] + u["output_tokens"] / 1e6 * price[1]
    return round(total, 4), sorted(unpriced)


STRUCTURED_MODES = ("output_config", "tool", "text")


def strict_schema(schema: Any) -> Any:
    """Copy of a schema with additionalProperties: false on every object (required by native mode)."""
    if isinstance(schema, dict):
        out = {k: strict_schema(v) for k, v in schema.items()}
        if out.get("type") == "object":
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


class UnsupportedMode(Exception):
    """The model rejected a structured-output mode; try the next one."""


def _is_mode_rejection(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    return status == 400 and any(
        s in msg for s in ("output_config", "output_format", "tool_choice", "not supported", "json_schema")
    )


class LLMClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.usage: list[dict] = []
        self._clients: dict[str, object] = {}
        self.structured_mode: dict[str, str] = {}  # model -> first mode that worked this run

    def _client(self, provider: str):
        if provider not in self._clients:
            key = self.config.secrets.key_for(provider)
            if not key:
                raise LLMError(f"no API key for provider '{provider}'")
            if provider == "anthropic":
                import anthropic

                self._clients[provider] = anthropic.Anthropic(api_key=key, max_retries=3, timeout=600)
            elif provider == "openai":
                import openai

                self._clients[provider] = openai.OpenAI(api_key=key, max_retries=3, timeout=600)
            else:
                raise LLMError(f"unknown provider '{provider}'")
        return self._clients[provider]

    def _anthropic(self, client, route: ModelRoute, system: str, prompt: str, max_tokens: int,
                   schema: dict | None, mode: str) -> LLMResult:
        kwargs: dict[str, Any] = {}
        if schema and mode == "output_config":
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": strict_schema(schema)}}
        elif schema and mode == "tool":
            kwargs["tools"] = [{"name": "respond", "description": "Return the answer.", "input_schema": schema}]
            kwargs["tool_choice"] = {"type": "tool", "name": "respond"}
        elif schema and mode == "text":
            system = system + "\n\nRespond with valid JSON only. No prose, no markdown fences."
        try:
            resp = client.messages.create(
                model=route.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}], **kwargs,
            )
        except Exception as exc:
            if schema and mode != "text" and _is_mode_rejection(exc):
                raise UnsupportedMode(str(exc)) from exc
            raise
        if schema and getattr(resp, "stop_reason", "") == "max_tokens":
            raise LLMError(f"structured output truncated at max_tokens={max_tokens}")
        if schema and mode == "tool":
            block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
            if block is None:
                raise LLMError("model did not return structured output")
            text = json.dumps(block.input)
        else:
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return LLMResult(text, "anthropic", route.model, resp.usage.input_tokens, resp.usage.output_tokens)

    def _call(self, route: ModelRoute, system: str, prompt: str, max_tokens: int,
              schema: dict | None = None) -> LLMResult:
        client = self._client(route.provider)
        if route.provider == "anthropic":
            if not schema:
                return self._anthropic(client, route, system, prompt, max_tokens, None, "text")
            known = self.structured_mode.get(route.model)
            modes = [known] if known else list(STRUCTURED_MODES)
            for mode in modes:
                try:
                    result = self._anthropic(client, route, system, prompt, max_tokens, schema, mode)
                except UnsupportedMode as exc:
                    log.info(f"{route.model} doesn't support structured mode '{mode}', trying the next: {exc}")
                    continue
                if route.model not in self.structured_mode:
                    self.structured_mode[route.model] = mode
                    log.info(f"{route.model}: using structured mode '{mode}'")
                return result
            raise LLMError(f"{route.model} supports none of the structured output modes")

        kwargs = {}
        if schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "respond", "schema": schema, "strict": False},
            }
        resp = client.chat.completions.create(
            model=route.model,
            max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            **kwargs,
        )
        usage = resp.usage
        return LLMResult(
            resp.choices[0].message.content or "",
            "openai",
            route.model,
            getattr(usage, "prompt_tokens", 0),
            getattr(usage, "completion_tokens", 0),
        )

    def complete(self, task: str, system: str, prompt: str, max_tokens: int = 4096, label: str = "",
                 schema: dict | None = None) -> LLMResult:
        llm = self.config.settings.llm
        route = llm.tasks[task]
        routes = [route] + ([llm.fallback] if llm.fallback and llm.fallback != route else [])
        errors = []
        for r in routes:
            try:
                result = self._call(r, system, prompt, max_tokens, schema)
            except Exception as exc:  # provider SDK errors, missing keys
                errors.append(f"{r.provider}/{r.model}: {exc}")
                log.warning(f"LLM {task}/{label or '-'} via {r.provider}/{r.model} failed: {exc}")
                continue
            self.usage.append({
                "task": task, "label": label, "provider": result.provider, "model": result.model,
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "fallback": r is not route,
            })
            return result
        raise LLMError(f"all providers failed for '{task}': " + " | ".join(errors))


class Memo:
    """Remembers completed LLM calls within one stage attempt.

    If a stage fails partway, the rerun reuses every call that already
    succeeded instead of paying for it again. The pipeline deletes the memo
    once the stage completes, so a deliberate rerun always gets fresh output.
    """

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}

    @staticmethod
    def key(*parts: Any) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def put(self, key: str, value: Any) -> None:
        self.data[key] = value
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        os.replace(tmp, self.path)


def complete_json(client, task: str, label: str, system: str, prompt: str, max_tokens: int = 4096,
                  schema: dict | None = None, memo: Memo | None = None) -> Any:
    """Structured JSON from the model (schema-enforced where the provider supports it).

    Falls back to parsing text, with one retry, if structured output isn't used.
    """
    key = Memo.key(task, label, system, prompt, schema) if memo else None
    if memo and (hit := memo.get(key)) is not None:
        return hit
    if not schema:
        system = system + "\n\nRespond with valid JSON only. No prose, no markdown fences."
    result = client.complete(task, system, prompt, max_tokens=max_tokens, label=label, schema=schema)
    try:
        value = parse_json(result.text)
    except ValueError as exc:
        log.warning(f"LLM {task}/{label}: invalid JSON ({exc}); retrying once")
        retry = (
            f"{prompt}\n\nYour previous reply was not valid JSON ({exc}). "
            "Return the complete answer again as valid JSON only."
        )
        result = client.complete(task, system, retry, max_tokens=max_tokens, label=f"{label}:retry", schema=schema)
        value = parse_json(result.text)
    used_fallback = bool(getattr(client, "usage", None)) and client.usage[-1].get("fallback")
    if memo and not used_fallback:
        # A fallback answer is never remembered: a resume should retry the primary model.
        memo.put(key, value)
    return value
