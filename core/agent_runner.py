"""Unified agent runner — routes to Claude Code CLI or LiteLLM based on config."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from core import config

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent

RUNTIME = config.get("llm.runtime", "claude-code")


# ---------------------------------------------------------------------------
# Claude Code CLI backend
# ---------------------------------------------------------------------------


def _run_claude_code(
    prompt: str,
    *,
    model: str = "sonnet",
    max_turns: int = 10,
    timeout_seconds: int = 120,
    allowed_tools: list[str] | None = None,
    cwd: str | None = None,
) -> str:
    cmd = [
        "claude",
        "--print",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
    ]

    if allowed_tools:
        for t in allowed_tools:
            cmd.extend(["--allowedTools", t])

    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=cwd or str(PROJECT_ROOT),
        )
        output = result.stdout.strip()
        if result.returncode != 0 and not output:
            logger.warning("claude exited %d: %s", result.returncode, result.stderr[:500])
            return result.stderr[:1000] if result.stderr else "Agent returned no output"
        return output
    except subprocess.TimeoutExpired:
        logger.error("claude timed out after %ds", timeout_seconds)
        return f"Agent timed out after {timeout_seconds}s"
    except FileNotFoundError:
        logger.error("'claude' CLI not found — falling back to litellm")
        return _run_litellm(prompt, model=_litellm_model(model), timeout_seconds=timeout_seconds)
    except Exception as e:
        logger.error("claude runner failed: %s", e)
        return f"Agent runner error: {e}"


# ---------------------------------------------------------------------------
# LiteLLM backend
# ---------------------------------------------------------------------------


def _litellm_model(short_name: str) -> str:
    """Map short names to full LiteLLM model identifiers."""
    mapping = {
        "sonnet": config.get("llm.default_model", "claude-sonnet-4-6"),
        "opus": config.get("llm.opus_model", "claude-opus-4-7"),
        "haiku": "claude-haiku-4-5-20251001",
    }
    return mapping.get(short_name, short_name)


def _run_litellm(
    prompt: str,
    *,
    model: str = "claude-sonnet-4-6",
    timeout_seconds: int = 120,
    **_kwargs,
) -> str:
    try:
        from langchain_community.chat_models import ChatLiteLLM
        from langchain_core.messages import HumanMessage

        llm = ChatLiteLLM(
            model=model,
            temperature=config.LLM_TEMPERATURE,
            max_tokens=config.LLM_MAX_TOKENS,
        )
        response = llm.invoke([HumanMessage(content=prompt)])
        return response.content
    except Exception as e:
        logger.error("litellm call failed: %s", e)
        return f"LiteLLM error: {e}"


# ---------------------------------------------------------------------------
# Public API — nodes call these
# ---------------------------------------------------------------------------


def run_node(
    prompt: str,
    node_name: str,
    *,
    allowed_tools: list[str] | None = None,
    cwd: str | None = None,
    timeout_seconds: int | None = None,
    runtime: str | None = None,
) -> str:
    """Run a prompt through the configured runtime for a given node.

    Args:
        prompt: The task prompt.
        node_name: Node name (used to pick model: opus vs sonnet).
        allowed_tools: Claude Code tool patterns (ignored for litellm).
        cwd: Working directory (Claude Code only).
        timeout_seconds: Override default timeout.
        runtime: Override config runtime ("claude-code" or "litellm").
    """
    is_opus = node_name in config.OPUS_NODES
    active_runtime = runtime or RUNTIME

    if active_runtime == "claude-code":
        cc_config = config.get("llm.claude_code", {}) or {}
        model = (
            config.get("llm.opus_model", "opus")
            if is_opus
            else config.get("llm.default_model", "sonnet")
        )
        timeout = timeout_seconds or (
            cc_config.get("opus_timeout", 300) if is_opus else cc_config.get("default_timeout", 120)
        )
        max_turns = cc_config.get("max_turns", 10)

        return _run_claude_code(
            prompt,
            model=model,
            max_turns=max_turns,
            timeout_seconds=timeout,
            allowed_tools=allowed_tools,
            cwd=cwd,
        )
    else:
        model = _litellm_model("opus" if is_opus else "sonnet")
        timeout = timeout_seconds or 120
        return _run_litellm(prompt, model=model, timeout_seconds=timeout)


def run_node_json(
    prompt: str,
    node_name: str,
    *,
    allowed_tools: list[str] | None = None,
    cwd: str | None = None,
    timeout_seconds: int | None = None,
    runtime: str | None = None,
) -> dict | list | None:
    """Run a prompt and parse the output as JSON."""
    raw = run_node(
        prompt,
        node_name,
        allowed_tools=allowed_tools,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        runtime=runtime,
    )

    if not raw:
        return None

    return _extract_json(raw)


def _extract_json(text: str) -> dict | list | None:
    """Extract JSON from text that may contain markdown fences or prose."""
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Markdown code fence
    for fence in ["```json", "```"]:
        if fence in text:
            start = text.index(fence) + len(fence)
            rest = text[start:]
            if "```" in rest:
                end = start + rest.index("```")
                try:
                    return json.loads(text[start:end].strip())
                except (json.JSONDecodeError, ValueError):
                    continue

    # Find first JSON object or array
    for open_c, close_c in [("{", "}"), ("[", "]")]:
        if open_c not in text:
            continue
        start = text.index(open_c)
        depth = 0
        for i in range(start, len(text)):
            if text[i] == open_c:
                depth += 1
            elif text[i] == close_c:
                depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    break
                break

    logger.warning("Could not extract JSON from agent output")
    return None
