"""
app/services/rag.py — dspy.RLM wrapper for document Q&A
========================================================
The single point of contact between this app and DSPy. Everything DSPy-
specific lives here. The router calls one async function and gets back
a typed answer + usage stats.

Why dspy.RLM and not classic vector-RAG?
  RLM (Recursive Language Model) treats the corpus as an external
  environment the LM explores via a Python REPL, rather than feeding
  pre-retrieved chunks into the prompt. Three real benefits:

    1. No embedding model, no vector DB. Less infra, less code.
    2. Multi-hop questions work natively — the LM can read one doc,
       extract a reference, then look it up in another doc.
    3. Long docs survive intact — no chunking artefacts.

  Trade-off: many LM sub-calls per question. We cap iterations and
  log usage so a runaway query can't bill us into oblivion.

Concurrency
-----------
dspy.RLM's docstring warns:
  "RLM instances are not thread-safe when using a custom interpreter.
   Create separate RLM instances for concurrent use, or use the default
   PythonInterpreter which creates a fresh instance per forward() call."

We build a NEW RLM per request. Cheap (it's just a Python object;
the heavy work is in forward()).

Why asyncio.to_thread?
----------------------
dspy.RLM.forward() is synchronous and blocks while LiteLLM does HTTP
calls under the hood. Calling it directly from our async route would
freeze the event loop for seconds. asyncio.to_thread() runs the call
in a thread pool so other requests keep flowing.
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass

import dspy
import structlog

from app.config import settings
from app.models.database import Document

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# One-time DSPy configuration
# ---------------------------------------------------------------------------
# dspy.configure() sets a process-wide LM. We do this lazily on first use
# rather than at import time, because:
#   1. Tests can override settings.ollama_* before our LM is built.
#   2. It avoids a network round-trip at import time if dspy's LM ever
#      eagerly validates the endpoint (it doesn't today, but the contract
#      isn't part of dspy's public API).
_lm_configured = False


def _ensure_lm_configured() -> None:
    """Configure dspy's global LM exactly once per process."""
    global _lm_configured
    if _lm_configured:
        return

    # LiteLLM provider format: "ollama_chat/<model>" routes through
    # Ollama's chat endpoint. "ollama/<model>" uses the older /generate
    # endpoint which doesn't support multi-turn — chat is the right pick.
    lm = dspy.LM(
        f"ollama_chat/{settings.ollama_model}",
        api_base=settings.ollama_base_url,
        api_key=settings.ollama_api_key or "",
    )
    dspy.configure(lm=lm)
    _lm_configured = True


# ---------------------------------------------------------------------------
# Public result type — keeps DSPy's Prediction out of the router
# ---------------------------------------------------------------------------


@dataclass
class RagAnswer:
    """What ask_documents() returns. Plain dataclass — never crosses HTTP directly."""

    answer: str
    iterations: int  # how many internal LM steps the RLM took
    total_tokens: int  # sum of tokens across all internal LM calls


# ---------------------------------------------------------------------------
# Building the corpus blob
# ---------------------------------------------------------------------------


def _build_corpus(documents: Iterable[Document]) -> str:
    """
    Concatenate documents into a single string the RLM loads into its sandbox.

    Each doc is wrapped in a clear delimiter so the LM can tell them apart
    when navigating with grep / slicing. The delimiter format is deliberately
    distinctive (===) so a stray "Document:" line inside a real doc can't be
    confused for a boundary.
    """
    parts: list[str] = []
    for doc in documents:
        parts.append(
            f"===== DOCUMENT id={doc.id} filename={doc.filename} =====\n"
            f"{doc.content_text}\n"
            f"===== END DOCUMENT id={doc.id} =====\n"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# Hard cap on internal LM calls per question. dspy.RLM default is 50.
# We set 20 — high enough for genuine multi-hop reasoning over our small
# corpus, low enough that one runaway question can't burn $10 in tokens.
# Tune in production based on observed iteration counts in logs.
MAX_LLM_CALLS = 20
MAX_ITERATIONS = 10


async def ask_documents(
    question: str,
    documents: list[Document],
) -> RagAnswer:
    """
    Run an RLM query over the given documents and return the answer.

    Args:
        question:  Natural-language question to answer.
        documents: Documents the RLM should be given access to. The full
                   content of each is loaded into the Pyodide sandbox.

    Returns:
        RagAnswer with the model's answer, iteration count, and token usage.

    Raises:
        ValueError: if documents is empty (caller should check first).
        Any DSPy/LiteLLM/Ollama errors propagate untouched — the router
        wraps them as HTTP 503. We deliberately do NOT swallow them here
        because they carry useful debug info (model name, latency, etc.).
    """
    if not documents:
        raise ValueError("ask_documents requires at least one document.")

    _ensure_lm_configured()

    corpus = _build_corpus(documents)

    # Build a fresh RLM per request (see "Concurrency" in the module docstring).
    # signature "context, question -> answer" defines the inputs/outputs the
    # LM sees. The RLM internally exposes `context` to the sandbox as a
    # Python variable the LM can grep/slice/recursively analyse.
    rlm = dspy.RLM(
        "context, question -> answer",
        max_iterations=MAX_ITERATIONS,
        max_llm_calls=MAX_LLM_CALLS,
    )

    # forward() blocks on HTTP — push to a worker thread so the FastAPI
    # event loop stays responsive to other requests.
    prediction = await asyncio.to_thread(
        rlm,
        context=corpus,
        question=question,
    )

    # dspy.Prediction exposes get_lm_usage() — a dict {model_name: {prompt_tokens,
    # completion_tokens, total_tokens, ...}}. Sum across all models that ran
    # during the recursive exploration (usually just one, but RLM can use
    # different sub-LMs).
    usage = prediction.get_lm_usage() or {}
    total_tokens = sum(
        int(model_usage.get("total_tokens", 0)) for model_usage in usage.values()
    )

    # iterations isn't a first-class attribute on Prediction — best effort.
    # dspy sometimes exposes it on internal trace; fall back to 0 if absent
    # rather than crashing the response.
    iterations = int(getattr(prediction, "iterations", 0))

    await logger.ainfo(
        "rlm answer produced",
        question_chars=len(question),
        corpus_chars=len(corpus),
        documents_used=[str(d.id) for d in documents],
        iterations=iterations,
        total_tokens=total_tokens,
    )

    return RagAnswer(
        answer=str(prediction.answer),
        iterations=iterations,
        total_tokens=total_tokens,
    )
