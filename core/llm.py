from __future__ import annotations
from functools import lru_cache

try:
    from langchain_litellm import ChatLiteLLM
except Exception:
    try:
        from langchain_community.chat_models import ChatLiteLLM
    except Exception:
        ChatLiteLLM = None

from core import config


@lru_cache(maxsize=8)
def get_llm(node_name: str) -> ChatLiteLLM | None:
    if node_name in config.NO_LLM_NODES:
        return None

    model = config.OPUS_MODEL if node_name in config.OPUS_NODES else config.DEFAULT_MODEL

    return ChatLiteLLM(
        model=model,
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS,
    )
