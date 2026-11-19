"""Chat models: one LangChain interface (``BaseChatModel``), three providers.

- ``fake``    ``FakeGroundedChatModel``: deterministic and offline; it "answers" with the
              first sentence of the first source and cites it. For tests and CI.
- ``ollama``  ``ChatOllama`` with Qwen3 8B on the local GPU (free, private).
- ``bedrock`` ``ChatBedrockConverse`` with Claude Haiku in staging (costs money).

LangGraph (Day 3) drives any ``BaseChatModel``, so the agent reuses this same factory.
≈ in Node, the Vercel AI SDK or LangChain.js: one ``generateText`` call, swappable providers.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_aws import ChatBedrockConverse
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_ollama import ChatOllama

from app.core.config import Settings

INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

_FIRST_SOURCE = re.compile(r'<source id="(\d+)"[^>]*>\n(.*?)\n</source>', re.DOTALL)


class FakeGroundedChatModel(BaseChatModel):
    """Answers with the first sentence of the first source, cited; no source -> no answer."""

    @property
    def _llm_type(self) -> str:
        return "fake-grounded"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        match = _FIRST_SOURCE.search(str(messages[-1].content))
        if match is None:
            text = INSUFFICIENT_EVIDENCE
        else:
            number, content = match.groups()
            body = content.split("\n\n", 1)[-1]  # drop the heading path
            text = f"{body.split('. ')[0].strip().rstrip('.')}. [{number}]"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Composition root for chat models: the only place that knows concrete classes."""
    match settings.llm_provider:
        case "fake":
            return FakeGroundedChatModel()
        case "ollama":
            return ChatOllama(
                model=settings.ollama_chat_model,
                base_url=settings.ollama_base_url,
                temperature=0,  # same question, same answer: easier to test and evaluate
                # Thinking tokens count too: give them room when reasoning is on.
                num_predict=settings.llm_max_output_tokens * (4 if settings.llm_reasoning else 1),
                # Qwen3 "thinking": better on tables and ranges, ~10x latency (LLM_REASONING).
                reasoning=settings.llm_reasoning,
            )
        case "bedrock":
            if settings.bedrock_chat_model_id is None:  # also rejected by Settings itself
                raise ValueError("LLM_PROVIDER=bedrock requires BEDROCK_CHAT_MODEL_ID")
            return ChatBedrockConverse(
                model_id=settings.bedrock_chat_model_id,
                region_name=settings.aws_region,
                credentials_profile_name=settings.aws_profile,
                temperature=0,
                max_tokens=settings.llm_max_output_tokens,
            )
