"""
The brain: whatever actually does the thinking.

Everything else in this project is a body — ears, hands, memory, the approval
gate. None of it understands anything. This module is the seam where an actual
language model gets plugged in, and it exists so that swapping OpenAI for
Claude for a local Ollama model is a one-line config change rather than a
rewrite.

Every provider is normalised down to the same two methods:

    chat(messages, system)  -> str          free-form conversation
    intent(utterance, ...)  -> dict         structured "what did they mean"

The second one is what makes phone control possible. "text mom I'm running
late" has to become {"action": "send_sms", "contact": "mom", ...} before any
hands can act on it, and getting that translation reliable is most of the
difficulty in a working assistant.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

import requests

log = logging.getLogger("jarvis.brain")


class BrainError(Exception):
    """Raised when the model can't be reached or returns something unusable."""


class Brain(Protocol):
    """The contract every provider implements."""

    def chat(self, messages: list[dict[str, str]], system: str) -> str: ...

    def intent(
        self,
        utterance: str,
        actions: list[dict[str, Any]],
        context: str = "",
    ) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
# Shared intent-extraction plumbing
# --------------------------------------------------------------------------

INTENT_SYSTEM = """\
You translate a spoken request into a single structured action for a phone \
assistant. You never chat, explain, apologise, or add commentary.

Reply with exactly one JSON object and nothing else:
{"action": "<name>", "params": {...}, "confidence": <0.0-1.0>, "speak": "<short confirmation>"}

Rules:
- "action" MUST be one of the available actions listed below, or "none".
- "confidence" is how sure you are you understood. Below 0.7 the assistant \
will ask the user to confirm before doing anything, so be honest — a low \
score is much cheaper than a wrong action.
- "speak" is one short sentence spoken aloud back to the user. No emoji.
- If the request is ambiguous, dangerous, or not covered by the available \
actions, use "none" and put the reason in "speak".
"""


def _strip_code_fence(text: str) -> str:
    """Pull JSON out of a ```json fence if the model wrapped it in one.

    Every model does this occasionally regardless of instructions, and it is
    far cheaper to tolerate than to fight with prompt engineering.
    """
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return fenced.group(1).strip() if fenced else text.strip()


def _parse_intent(raw: str) -> dict[str, Any]:
    """Turn a model response into a validated intent dict.

    Anything unparseable degrades to a low-confidence "none" rather than
    raising. A garbled response should make Jarvis ask you to repeat yourself,
    not crash the server mid-sentence.
    """
    cleaned = _strip_code_fence(raw)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: find the outermost brace pair. Models sometimes emit a
        # stray sentence before the JSON.
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            log.warning("intent response had no JSON: %r", raw[:200])
            return _none_intent("I didn't catch that — say it again?")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            log.warning("intent JSON malformed: %r", raw[:200])
            return _none_intent("I didn't catch that — say it again?")

    if not isinstance(parsed, dict):
        return _none_intent("I didn't catch that — say it again?")

    action = parsed.get("action")
    if not isinstance(action, str) or not action:
        return _none_intent("I'm not sure what you want me to do.")

    params = parsed.get("params")
    if not isinstance(params, dict):
        params = {}

    # Clamp rather than reject. A model that returns confidence: 5 meant
    # "very sure", and throwing away an otherwise-good intent over a range
    # error would be pedantic.
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    speak = parsed.get("speak")
    if not isinstance(speak, str):
        speak = ""

    return {
        "action": action,
        "params": params,
        "confidence": confidence,
        "speak": speak,
    }


def _none_intent(reason: str) -> dict[str, Any]:
    return {"action": "none", "params": {}, "confidence": 0.0, "speak": reason}


def _build_intent_prompt(
    utterance: str,
    actions: list[dict[str, Any]],
    context: str,
) -> str:
    lines = ["Available actions:"]
    for spec in actions:
        params = ", ".join(spec.get("params", []))
        lines.append(f"- {spec['name']}({params}): {spec['description']}")

    if context:
        lines.append(f"\nContext about the user:\n{context}")

    lines.append(f"\nRequest: {utterance}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------

class OpenAIBrain:
    def __init__(self, api_key: str, model: str, max_tokens: int = 2048):
        if not api_key:
            raise BrainError("OpenAI selected but no api_key set in config.yaml")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise BrainError("openai package missing — pip install openai") from exc

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def _call(self, messages: list[dict[str, str]], max_tokens: int) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise BrainError(f"OpenAI call failed: {exc}") from exc

        content = response.choices[0].message.content
        if not content:
            raise BrainError("OpenAI returned an empty response")
        return content

    def chat(self, messages: list[dict[str, str]], system: str) -> str:
        return self._call(
            [{"role": "system", "content": system}, *messages],
            self._max_tokens,
        )

    def intent(self, utterance, actions, context="") -> dict[str, Any]:
        raw = self._call(
            [
                {"role": "system", "content": INTENT_SYSTEM},
                {
                    "role": "user",
                    "content": _build_intent_prompt(utterance, actions, context),
                },
            ],
            # Intents are one small JSON object. Capping tight keeps latency
            # and cost down on the hot path.
            512,
        )
        return _parse_intent(raw)


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------

class AnthropicBrain:
    def __init__(self, api_key: str, model: str, max_tokens: int = 2048):
        if not api_key:
            raise BrainError("Anthropic selected but no api_key set in config.yaml")
        try:
            import anthropic
        except ImportError as exc:
            raise BrainError("anthropic package missing — pip install anthropic") from exc

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def _call(self, messages, system: str, max_tokens: int) -> str:
        try:
            response = self._client.messages.create(
                model=self._model,
                system=system,
                messages=messages,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise BrainError(f"Anthropic call failed: {exc}") from exc

        parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        if not parts:
            raise BrainError("Anthropic returned no text content")
        return "".join(parts)

    def chat(self, messages: list[dict[str, str]], system: str) -> str:
        return self._call(messages, system, self._max_tokens)

    def intent(self, utterance, actions, context="") -> dict[str, Any]:
        raw = self._call(
            [{
                "role": "user",
                "content": _build_intent_prompt(utterance, actions, context),
            }],
            INTENT_SYSTEM,
            512,
        )
        return _parse_intent(raw)


# --------------------------------------------------------------------------
# Ollama (local, free)
# --------------------------------------------------------------------------

class OllamaBrain:
    def __init__(self, host: str, model: str):
        self._host = host.rstrip("/")
        self._model = model

    def _call(self, messages: list[dict[str, str]], json_mode: bool = False) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if json_mode:
            # Ollama can constrain decoding to valid JSON. Small local models
            # need this — without it they narrate around the object.
            payload["format"] = "json"

        try:
            response = requests.post(
                f"{self._host}/api/chat",
                json=payload,
                timeout=120,  # local models on CPU are genuinely this slow
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise BrainError(
                f"Ollama unreachable at {self._host} — is `ollama serve` running? ({exc})"
            ) from exc

        content = response.json().get("message", {}).get("content", "")
        if not content:
            raise BrainError("Ollama returned an empty response")
        return content

    def chat(self, messages: list[dict[str, str]], system: str) -> str:
        return self._call([{"role": "system", "content": system}, *messages])

    def intent(self, utterance, actions, context="") -> dict[str, Any]:
        raw = self._call(
            [
                {"role": "system", "content": INTENT_SYSTEM},
                {
                    "role": "user",
                    "content": _build_intent_prompt(utterance, actions, context),
                },
            ],
            json_mode=True,
        )
        return _parse_intent(raw)


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------

def build_brain(config: dict[str, Any]) -> Brain:
    """Construct the configured brain, or explain clearly why we can't."""
    provider = config.get("provider", "").lower().strip()

    if provider == "openai":
        cfg = config.get("openai", {})
        return OpenAIBrain(
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", "gpt-4o"),
            max_tokens=cfg.get("max_tokens", 2048),
        )

    if provider == "anthropic":
        cfg = config.get("anthropic", {})
        return AnthropicBrain(
            api_key=cfg.get("api_key", ""),
            model=cfg.get("model", "claude-sonnet-5"),
            max_tokens=cfg.get("max_tokens", 2048),
        )

    if provider == "ollama":
        cfg = config.get("ollama", {})
        return OllamaBrain(
            host=cfg.get("host", "http://localhost:11434"),
            model=cfg.get("model", "llama3.1:8b"),
        )

    raise BrainError(
        f"unknown brain provider {provider!r} — "
        f"set brain.provider to 'openai', 'anthropic', or 'ollama'"
    )
