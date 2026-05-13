"""Custom OpenAI relay aliases for config `class_path` providers."""

from __future__ import annotations

from langchain_openai import ChatOpenAI


class RelayChatOpenAI(ChatOpenAI):
    """Thin `ChatOpenAI` subclass for custom config provider aliases."""

