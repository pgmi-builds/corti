"""Embedding provider adapters (one provider per file).


Public surface:

- :class:`EmbeddingProvider` — Protocol every provider satisfies.
- :class:`EmbeddingServiceError` — provider-side failure.
- :class:`EmbeddingError` — backward-compat alias for ``EmbeddingServiceError``.
- :class:`EmbedGuard` — cooldown gate for a provider believed to be down.
- :class:`OpenAIEmbeddingProvider` — concrete provider for any
  OpenAI-protocol embeddings endpoint (DeepInfra, vLLM, OpenAI, …).
- :func:`build_embedding_provider` — settings-driven factory.

External usage::

    from corti.component.embedding import build_embedding_provider
    provider = build_embedding_provider(settings.embedding)
    vec = await provider.embed("hello")
"""

from corti.core.errors import EmbeddingServiceError as EmbeddingServiceError

from .accessor import EmbeddingNotConfiguredError as EmbeddingNotConfiguredError
from .accessor import get_embedder as get_embedder
from .factory import build_embedding_provider as build_embedding_provider
from .guard import EmbedGuard as EmbedGuard
from .openai_provider import OpenAIEmbeddingProvider as OpenAIEmbeddingProvider
from .protocol import EmbeddingError as EmbeddingError
from .protocol import EmbeddingProvider as EmbeddingProvider

__all__ = [
    "EmbedGuard",
    "EmbeddingError",
    "EmbeddingProvider",
    "EmbeddingServiceError",
    "OpenAIEmbeddingProvider",
    "build_embedding_provider",
    "get_embedder",
]
