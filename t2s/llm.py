"""
llm.py -- thin provider abstraction so the rest of the code never imports an SDK.

Why structured output via a forced tool call?
  The model must return a machine-readable decision {answer | clarify | refuse} plus SQL.
  Forcing a tool call (tool_choice) makes the API return validated JSON, so we never parse
  free text or strip ``` fences. Alternative considered: asking for JSON in the prompt and
  regex-parsing -- more brittle, more retries.

Providers (choose with env T2SQL_PROVIDER=anthropic|azure, see make_llm):
  AnthropicLLM  -- real model (default model from env T2SQL_MODEL).
  AzureOpenAILLM -- Azure OpenAI GPT deployment.
  ScriptedLLM   -- deterministic fake used by unit tests and to test the pipeline offline.
Adding another provider (OpenAI-compatible / local Ollama) = implement `complete()`.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol


# Rate-limit (429) retries with exponential back-off are done by the SDK. Free/low-quota deployments need more
# patience than the default, so this is configurable: T2SQL_MAX_RETRIES in .env (default 8).
MAX_RETRIES = int(os.environ.get("T2SQL_MAX_RETRIES", "8"))


@dataclass
class LLMOutput:
    data: dict[str, Any]          # parsed tool input (the structured answer)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str, tool: dict) -> LLMOutput: ...


# ----------------------------------------------------------------------------------------
class AnthropicLLM:
    def __init__(self, model: str | None = None, temperature: float | None = 0.0, max_tokens: int = 1024):
        import anthropic  # imported lazily so tests run without the SDK/key
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=MAX_RETRIES)     # SDK handles 429/5xx back-off
        self.name = model or os.environ.get("T2SQL_MODEL", "claude-sonnet-5-5")
        self.temperature = temperature       # 0 => (near-)deterministic, important for evaluation
        self.max_tokens = max_tokens

    def complete(self, system: str, user: str, tool: dict) -> LLMOutput:
        kwargs: dict[str, Any] = dict(
            model=self.name,
            max_tokens=self.max_tokens,
            # cache_control: the long static system prompt + schema is identical across requests,
            # so prompt caching cuts cost/latency for the eval run (ignored if below min size).
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
        )
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        t0 = time.monotonic()
        try:
            resp = self.client.messages.create(**kwargs)
        except self._anthropic.BadRequestError as e:
            # Some newer models reject an explicit temperature; retry once without it.
            if "temperature" in str(e).lower() and "temperature" in kwargs:
                kwargs.pop("temperature")
                resp = self.client.messages.create(**kwargs)
            else:
                raise
        block = next(b for b in resp.content if b.type == "tool_use")
        return LLMOutput(dict(block.input), resp.usage.input_tokens, resp.usage.output_tokens,
                         time.monotonic() - t0)


# ----------------------------------------------------------------------------------------
class AzureOpenAILLM:
    """Azure OpenAI (GPT) provider. Same contract as AnthropicLLM: forced function call -> validated JSON.

    Env vars:  AZURE_OPENAI_ENDPOINT   e.g. https://my-resource.openai.azure.com/
               AZURE_OPENAI_API_KEY
               AZURE_OPENAI_DEPLOYMENT the DEPLOYMENT NAME you created in Azure (not the model family)
               AZURE_OPENAI_API_VERSION (default 2024-10-21)
    """

    def __init__(self, deployment: str | None = None, temperature: float | None = 0.0, max_tokens: int = 1024):
        import openai  # lazy import
        self._openai = openai
        self.client = openai.AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            max_retries=MAX_RETRIES,
        )
        self.name = deployment or os.environ["AZURE_OPENAI_DEPLOYMENT"]
        self.temperature, self.max_tokens = temperature, max_tokens

    @staticmethod
    def _to_openai_tool(tool: dict) -> dict:
        # Our schema is Anthropic-style (input_schema); OpenAI calls it `parameters`.
        return {"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                                                  "parameters": tool["input_schema"]}}

    def complete(self, system: str, user: str, tool: dict) -> LLMOutput:
        import json
        kwargs: dict[str, Any] = dict(
            model=self.name,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[self._to_openai_tool(tool)],
            tool_choice={"type": "function", "function": {"name": tool["name"]}},
            max_completion_tokens=self.max_tokens,
        )
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        t0 = time.monotonic()
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except self._openai.BadRequestError as e:
            msg = str(e).lower()
            if "temperature" in msg and "temperature" in kwargs:       # reasoning models reject temperature
                kwargs.pop("temperature")
                resp = self.client.chat.completions.create(**kwargs)
            elif "max_completion_tokens" in msg:                        # older API versions
                kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise
        call = resp.choices[0].message.tool_calls[0]
        return LLMOutput(json.loads(call.function.arguments), resp.usage.prompt_tokens,
                         resp.usage.completion_tokens, time.monotonic() - t0)


class OpenAICompatLLM:
    """Any OpenAI-compatible chat endpoint: Google Gemini, Groq, OpenRouter, a local Ollama server, ...

    Env vars:  OPENAI_COMPAT_BASE_URL  e.g. https://generativelanguage.googleapis.com/v1beta/openai/
               OPENAI_COMPAT_API_KEY   (Ollama: any non-empty text, e.g. "ollama")
               OPENAI_COMPAT_MODEL     e.g. a Gemini / Llama model id from the provider's docs

    Uses the same forced function call as the other providers. Some providers/models ignore a forced
    tool call, so if no tool call comes back we fall back to parsing JSON from the message text.
    """

    def __init__(self, model: str | None = None, temperature: float | None = 0.0, max_tokens: int = 1024):
        import openai
        self._openai = openai
        self.client = openai.OpenAI(base_url=os.environ["OPENAI_COMPAT_BASE_URL"],
                                    api_key=os.environ["OPENAI_COMPAT_API_KEY"], max_retries=MAX_RETRIES)
        self.name = model or os.environ["OPENAI_COMPAT_MODEL"]
        self.temperature, self.max_tokens = temperature, max_tokens

    def complete(self, system: str, user: str, tool: dict) -> LLMOutput:
        import json, re
        kwargs: dict[str, Any] = dict(
            model=self.name,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[AzureOpenAILLM._to_openai_tool(tool)],
            tool_choice={"type": "function", "function": {"name": tool["name"]}},
            max_tokens=self.max_tokens,
        )
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        t0 = time.monotonic()
        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        if getattr(msg, "tool_calls", None):
            data = json.loads(msg.tool_calls[0].function.arguments)
        else:                                            # fallback: JSON in plain text
            text = msg.content or ""
            m = re.search(r"\{.*\}", text, flags=re.S)
            if not m:
                raise ValueError("model returned neither a tool call nor JSON")
            data = json.loads(m.group(0))
        usage = getattr(resp, "usage", None)
        return LLMOutput(data, getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0,
                         time.monotonic() - t0)


def make_llm(temperature: float | None = 0.0) -> "LLM":
    """Pick the provider from env T2SQL_PROVIDER = anthropic (default) | azure | openai_compat."""
    provider = os.environ.get("T2SQL_PROVIDER", "anthropic").lower()
    needed = {"azure": ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"],
              "openai_compat": ["OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL"],
              }.get(provider, ["ANTHROPIC_API_KEY"])
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:   # name the variables only -- never echo secret values
        raise SystemExit(f"Missing {', '.join(missing)}. Copy .env.example to .env and fill it in.")
    if provider == "azure":
        return AzureOpenAILLM(temperature=temperature)
    if provider == "openai_compat":
        return OpenAICompatLLM(temperature=temperature)
    return AnthropicLLM(temperature=temperature)


# ----------------------------------------------------------------------------------------
class ScriptedLLM:
    """Deterministic stand-in for tests: `script` maps (call_index, user_text) -> dict."""

    name = "scripted"

    def __init__(self, fn: Callable[[int, str, str], dict]):
        self.fn, self.calls = fn, 0

    def complete(self, system: str, user: str, tool: dict) -> LLMOutput:
        out = self.fn(self.calls, user, tool["name"])
        self.calls += 1
        return LLMOutput(out)
