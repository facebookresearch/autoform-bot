# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""LLM client factory with provider registry."""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Any

from .protocol import InferenceProtocol, ModelPricing

# ---------------------------------------------------------------------------
# Backend enum — determines which SDK/inference class to use
# ---------------------------------------------------------------------------


class Backend(StrEnum):
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OPENAI_RESPONSE = "openai_response"
    OPENAI_CHAT = "openai_chat"
    ARISTOTLE = "aristotle"

    def create(
        self, *, api_key: str, url: str, model_name: str, is_local: bool, pricing: ModelPricing | None = None
    ) -> InferenceProtocol:
        """Construct the inference backend for this enum member.

        Imports are lazy so that only the required SDK is loaded.
        """
        match self:
            case Backend.ANTHROPIC:
                from anthropic import AsyncAnthropic

                from .sdk.anthropic import AnthropicInference

                client = AsyncAnthropic(api_key=api_key)
                return AnthropicInference(client, model_name=model_name)

            case Backend.GEMINI:
                from google import genai

                from .sdk.gemini import GeminiInference

                client = genai.Client(api_key=api_key)
                return GeminiInference(client, model_name=model_name)

            case Backend.OPENAI_RESPONSE:
                from openai import AsyncOpenAI

                from .openai_response_api import OpenAIResponseInference

                client = AsyncOpenAI(api_key=api_key, base_url=url)
                return OpenAIResponseInference(client, model_name=model_name)

            case Backend.OPENAI_CHAT:
                from openai import AsyncOpenAI

                from .openai_api import OpenAIChatInference

                client = AsyncOpenAI(api_key=api_key, base_url=url)
                return OpenAIChatInference(client, model_name=model_name, is_local=is_local, pricing=pricing)

            case Backend.ARISTOTLE:
                import aristotlelib

                from .sdk.aristotle import AristotleInference

                # aristotlelib reads the key from a module-global / env var.
                aristotlelib.set_api_key(api_key)
                return AristotleInference(model_name=model_name)

            case _:
                raise ValueError(f"Unknown backend: {self}")


# ---------------------------------------------------------------------------
# Model base class
# ---------------------------------------------------------------------------


class Model:
    """Base class for model definitions.

    Each subclass represents a model with its provider URL, API key env var,
    backend, and pricing. Subclasses auto-register in ``Model._registry``.
    """

    model_name: str
    abbreviation: str
    pricing: ModelPricing
    provider_url: str
    env_key: str
    backend: Backend
    is_local: bool = False

    _registry: list[type[Model]] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if hasattr(cls, "model_name"):
            Model._registry.append(cls)


# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------

_OPENAI_API = "https://api.openai.com/v1/"
_OLLAMA_LOCAL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
_VLLM_LOCAL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")

_FREE = ModelPricing(input_cost_per_m=0.0, output_cost_per_m=0.0)


# ---------------------------------------------------------------------------
# Anthropic models
# ---------------------------------------------------------------------------


class Haiku_4_5(Model):
    model_name = "claude-haiku-4-5-20251001"
    abbreviation = "Haiku 4.5"
    pricing = ModelPricing(
        input_cost_per_m=1.0, output_cost_per_m=5.0, cached_input_cost_per_m=0.1, cache_write_cost_per_m=1.25
    )
    provider_url = ""
    env_key = "ANTHROPIC_API_KEY"
    backend = Backend.ANTHROPIC


class Sonnet_4_6(Model):
    model_name = "claude-sonnet-4-6"
    abbreviation = "Sonnet 4.6"
    pricing = ModelPricing(
        input_cost_per_m=3.0, output_cost_per_m=15.0, cached_input_cost_per_m=0.3, cache_write_cost_per_m=3.75
    )
    provider_url = ""
    env_key = "ANTHROPIC_API_KEY"
    backend = Backend.ANTHROPIC


class Opus_4_6(Model):
    model_name = "claude-opus-4-6"
    abbreviation = "Opus 4.6"
    pricing = ModelPricing(
        input_cost_per_m=5.0, output_cost_per_m=25.0, cached_input_cost_per_m=0.5, cache_write_cost_per_m=6.25
    )
    provider_url = ""
    env_key = "ANTHROPIC_API_KEY"
    backend = Backend.ANTHROPIC


class Opus_4_7(Model):
    model_name = "claude-opus-4-7"
    abbreviation = "Opus 4.7"
    pricing = ModelPricing(
        input_cost_per_m=5.5, output_cost_per_m=27.5, cached_input_cost_per_m=0.55, cache_write_cost_per_m=6.875
    )
    provider_url = ""
    env_key = "ANTHROPIC_API_KEY"
    backend = Backend.ANTHROPIC


# ---------------------------------------------------------------------------
# OpenAI models
# ---------------------------------------------------------------------------


class GPT_5_4_Nano(Model):
    model_name = "gpt-5.4-nano"
    abbreviation = "GPT 5.4 Nano"
    pricing = ModelPricing(input_cost_per_m=0.2, output_cost_per_m=1.25, cached_input_cost_per_m=0.02)
    provider_url = _OPENAI_API
    env_key = "OPENAI_API_KEY"
    backend = Backend.OPENAI_RESPONSE


class GPT_5_4(Model):
    model_name = "gpt-5.4"
    abbreviation = "GPT 5.4"
    pricing = ModelPricing(input_cost_per_m=2.5, output_cost_per_m=15.0, cached_input_cost_per_m=0.25)
    provider_url = _OPENAI_API
    env_key = "OPENAI_API_KEY"
    backend = Backend.OPENAI_RESPONSE


class GPT_5_4_Pro(Model):
    model_name = "gpt-5.4-pro"
    abbreviation = "GPT 5.4 Pro"
    pricing = ModelPricing(input_cost_per_m=30.0, output_cost_per_m=180.0)
    provider_url = _OPENAI_API
    env_key = "OPENAI_API_KEY"
    backend = Backend.OPENAI_RESPONSE


# ---------------------------------------------------------------------------
# Gemini models
# ---------------------------------------------------------------------------


class Gemini_3_1_Flash_Lite(Model):
    model_name = "gemini-3.1-flash-lite-preview"
    abbreviation = "Gemini 3.1 Flash Lite"
    pricing = ModelPricing(input_cost_per_m=0.25, output_cost_per_m=1.5)
    provider_url = ""
    env_key = "GEMINI_API_KEY"
    backend = Backend.GEMINI


class Gemini_3_1_Pro(Model):
    model_name = "gemini-3.1-pro-preview"
    abbreviation = "Gemini 3.1 Pro"
    pricing = ModelPricing(input_cost_per_m=2.0, output_cost_per_m=12.0)
    provider_url = ""
    env_key = "GEMINI_API_KEY"
    backend = Backend.GEMINI


# ---------------------------------------------------------------------------
# Aristotle (Harmonic) — autonomous formal-reasoning agent, not a chat model.
# See core/inference/sdk/aristotle.py for the job-based integration and its
# limitations (no per-turn tool calls, no in-flight steering, no token usage).
# ---------------------------------------------------------------------------


class Aristotle(Model):
    model_name = "aristotle"
    abbreviation = "Aristotle"
    # Aristotle bills by compute, not tokens, so per-token pricing does not
    # apply; report zero so cost accounting treats it as untracked.
    pricing = _FREE
    provider_url = ""  # aristotlelib targets its own base URL internally
    env_key = "ARISTOTLE_API_KEY"
    backend = Backend.ARISTOTLE


# ---------------------------------------------------------------------------
# Ollama models (local)
# ---------------------------------------------------------------------------


class Ollama_Qwen3_5(Model):
    model_name = "qwen3.5"
    abbreviation = "Ollama Qwen 3.5"
    pricing = _FREE
    provider_url = _OLLAMA_LOCAL
    env_key = "OLLAMA_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


class Ollama_Qwen3_5_35B(Model):
    model_name = "qwen3.5:35b"
    abbreviation = "Ollama Qwen 3.5 35B"
    pricing = _FREE
    provider_url = _OLLAMA_LOCAL
    env_key = "OLLAMA_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


class Ollama_GLM_4_7_Flash(Model):
    model_name = "glm-4.7-flash"
    abbreviation = "Ollama GLM 4.7 Flash"
    pricing = _FREE
    provider_url = _OLLAMA_LOCAL
    env_key = "OLLAMA_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


class Ollama_GPT_OSS(Model):
    model_name = "gpt-oss"
    abbreviation = "Ollama GPT-OSS"
    pricing = _FREE
    provider_url = _OLLAMA_LOCAL
    env_key = "OLLAMA_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


class Ollama_GPT_OSS_120B(Model):
    model_name = "gpt-oss:120b"
    abbreviation = "Ollama GPT-OSS 120B"
    pricing = _FREE
    provider_url = _OLLAMA_LOCAL
    env_key = "OLLAMA_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


# ---------------------------------------------------------------------------
# vLLM models (local)
# ---------------------------------------------------------------------------


class VLLM_Qwen3_5_4B(Model):
    model_name = "Qwen/Qwen3.5-4B"
    abbreviation = "vLLM Qwen 3.5"
    pricing = _FREE
    provider_url = _VLLM_LOCAL
    env_key = "VLLM_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


class VLLM_Qwen3_5_35B_A3B(Model):
    model_name = "Qwen/Qwen3.5-35B-A3B"
    abbreviation = "vLLM Qwen 3.5 35B"
    pricing = _FREE
    provider_url = _VLLM_LOCAL
    env_key = "VLLM_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


class VLLM_GLM_4_7_Flash(Model):
    model_name = "zai-org/GLM-4.7-Flash"
    abbreviation = "vLLM GLM 4.7 Flash"
    pricing = _FREE
    provider_url = _VLLM_LOCAL
    env_key = "VLLM_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


class VLLM_GPT_OSS_20B(Model):
    model_name = "openai/gpt-oss-20b"
    abbreviation = "vLLM GPT-OSS"
    pricing = _FREE
    provider_url = _VLLM_LOCAL
    env_key = "VLLM_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


class VLLM_GPT_OSS_120B(Model):
    model_name = "openai/gpt-oss-120b"
    abbreviation = "vLLM GPT-OSS 120B"
    pricing = _FREE
    provider_url = _VLLM_LOCAL
    env_key = "VLLM_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


class VLLM_Leanstral(Model):
    model_name = "mistralai/Leanstral-2603"
    abbreviation = "vLLM Leanstral"
    pricing = _FREE
    provider_url = _VLLM_LOCAL
    env_key = "VLLM_API_KEY"
    backend = Backend.OPENAI_CHAT
    is_local = True


# ---------------------------------------------------------------------------
# Default model and helpers
# ---------------------------------------------------------------------------

DEFAULT_MODEL: type[Model] = Opus_4_6
_ALL_MODELS: tuple[type[Model], ...] = tuple(Model._registry)
_MODEL_BY_ABBR: dict[str, type[Model]] = {m.abbreviation: m for m in _ALL_MODELS}


def lookup_model(abbreviation: str) -> type[Model]:
    """Find a Model class by abbreviation from the global registry."""
    if abbreviation in _MODEL_BY_ABBR:
        return _MODEL_BY_ABBR[abbreviation]
    valid = ", ".join(m.abbreviation for m in _ALL_MODELS)
    raise ValueError(f"Model {abbreviation!r} is not available. Available: {valid}")


# ---------------------------------------------------------------------------
# Register pricing (populates core.inference._MODEL_PRICING_REGISTRY)
# ---------------------------------------------------------------------------

for _m in _ALL_MODELS:
    ModelPricing.register(_m.abbreviation, _m.pricing)
    ModelPricing.register(_m.model_name, _m.pricing)
del _m


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def create_inference(model: type[Model]) -> InferenceProtocol:
    """Create the appropriate inference backend for a Model class.

    Resolves the API key from the environment and delegates construction
    to ``model.backend.create()``.
    """
    api_key = os.environ.get(model.env_key, "")
    if not api_key:
        if model.is_local:
            api_key = "no-key"
        else:
            raise RuntimeError(f"Set {model.env_key}")

    return model.backend.create(
        api_key=api_key,
        url=model.provider_url,
        model_name=model.model_name,
        is_local=model.is_local,
        pricing=model.pricing,
    )
