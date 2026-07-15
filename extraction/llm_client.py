"""
extraction/llm_client.py — LLM client Protocol and OpenRouter implementation.

spec.md §3 (Engineering Standards): interface-first design for anything that
talks to an external system — define a Protocol before the concrete implementation.

The Protocol (LLMClient) is what the agent depends on, making it trivially
mockable in unit tests without patching httpx internals.

Concrete implementation: OpenRouterClient
  - Uses httpx (sync) for simplicity at Phase 2.
  - Calls the OpenAI-compatible /chat/completions endpoint.
  - Fallback chain (tried in order, each model retried once on 429):
      1. openrouter/auto          — OpenRouter meta-router (free, picks best model)
      2. google/gemma-3-27b-it:free
      3. qwen/qwen3-235b-a22b:free
      4. deepseek/deepseek-r1-0528:free
  - On 429: reads Retry-After header, sleeps that many seconds, retries the
    same model once.  If the retry is still 429, advances to the next model.
  - If every model in the chain is rate-limited, raises LLMCallError(status_code=429).
  - Never logs the API key (spec.md §4 — secrets never logged).
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# Default timeouts (seconds) — keep under spec.md NFR 30s latency budget.
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0

# Fallback chain used when no explicit override is passed.
# Tried in order; each model gets one automatic retry on 429 before the next
# model in the chain is attempted.
_DEFAULT_FALLBACK_CHAIN: list[str] = [
    "openrouter/auto",              # 1st: OpenRouter meta-router (picks best free model)
    "google/gemma-3-27b-it:free",   # 2nd: Gemma 3 27B (explicit free fallback)
    "qwen/qwen3-235b-a22b:free",    # 3rd: Qwen3 235B A22B
    "deepseek/deepseek-r1-0528:free",  # 4th: DeepSeek R1 0528
]

# Maximum seconds to sleep on a single Retry-After header.
# Capped so a rogue server cannot block us for minutes.
_MAX_RETRY_AFTER_SECONDS = 60.0


class LLMCallError(Exception):
    """Raised when the LLM API call fails for any reason."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class LLMClient(Protocol):
    """
    Protocol for any LLM client used by the extraction agent.

    Concrete implementations must provide exactly this method so the agent
    can be tested with any mock that satisfies the protocol.
    """

    def complete(self, system_prompt: str, user_message: str) -> str:
        """
        Send a chat-completion request and return the assistant's text content.

        Args:
            system_prompt: The system-role message (prompt instructions).
            user_message:  The user-role message (invoice text or retry payload).

        Returns:
            The raw text content of the model's first choice.

        Raises:
            LLMCallError: on any network, timeout, or API-level error.
        """
        ...


def _parse_retry_after(response: httpx.Response) -> float:
    """
    Extract the Retry-After value (in seconds) from a 429 response.

    OpenRouter returns it both as a top-level response header and nested inside
    the JSON error body under error.metadata.retry_after_seconds.  We prefer
    the JSON value (more precise) and fall back to the header.

    Returns a float clamped to [0, _MAX_RETRY_AFTER_SECONDS].
    """
    # Try JSON body first (OpenRouter-specific, sub-second precision).
    try:
        body = response.json()
        metadata = body.get("error", {}).get("metadata", {})
        json_seconds = metadata.get("retry_after_seconds") or metadata.get("retry_after_seconds_raw")
        if json_seconds is not None:
            return max(0.0, min(float(json_seconds), _MAX_RETRY_AFTER_SECONDS))
    except Exception:  # noqa: BLE001
        pass

    # Fall back to the standard HTTP header.
    header_val = response.headers.get("retry-after", "").strip()
    if header_val:
        try:
            return max(0.0, min(float(header_val), _MAX_RETRY_AFTER_SECONDS))
        except ValueError:
            pass

    # Default: wait 10 seconds if no hint is provided.
    return 10.0


class OpenRouterClient:
    """
    Concrete LLM client that calls OpenRouter's /chat/completions endpoint.

    Satisfies the LLMClient Protocol.

    Fallback behaviour
    ------------------
    Each model in ``fallback_chain`` is tried in order.  On HTTP 429 the
    client reads the ``Retry-After`` value (from the JSON body or the response
    header), sleeps that many seconds, and retries **once**.  If the retry is
    also 429 the next model in the chain is tried.  If the entire chain is
    exhausted the final 429 is re-raised as ``LLMCallError(status_code=429)``.

    Args:
        settings:       Application settings (supplies API key and base URL).
        fallback_chain: Ordered list of OpenRouter model slugs to try.
                        Defaults to ``_DEFAULT_FALLBACK_CHAIN``.
        timeout:        httpx Timeout object; defaults to connect=10s, read=60s.
    """

    def __init__(
        self,
        settings: Settings,
        fallback_chain: list[str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._api_key = settings.openrouter_api_key
        self._base_url = settings.openrouter_base_url.rstrip("/")
        self._fallback_chain: list[str] = fallback_chain or list(settings.openrouter_fallback_chain)
        self._timeout = timeout or httpx.Timeout(
            connect=_CONNECT_TIMEOUT,
            read=_READ_TIMEOUT,
            write=10.0,
            pool=10.0,
        )

    # ------------------------------------------------------------------
    # Public API — satisfies LLMClient Protocol
    # ------------------------------------------------------------------

    def complete(self, system_prompt: str, user_message: str) -> str:
        """
        Call OpenRouter /chat/completions, walking the fallback chain on 429.

        Returns:
            The assistant's text content from the first successful response.

        Raises:
            LLMCallError: if every model in the chain is rate-limited (429) or
                          any model returns an unrecoverable error.
        """
        last_error: LLMCallError | None = None

        for model in self._fallback_chain:
            try:
                content = self._call_with_one_retry(model, system_prompt, user_message)
                return content
            except LLMCallError as exc:
                last_error = exc
                if exc.status_code == 429:
                    # 429 after retry — try next model in chain.
                    logger.warning(
                        "Model rate-limited after retry — advancing to next fallback",
                        extra={"model": model},
                    )
                    continue
                # Any other error (5xx, auth, bad shape) — propagate immediately.
                raise

        # All models exhausted.
        raise LLMCallError(
            "All models in the fallback chain returned 429. "
            f"Chain: {self._fallback_chain}",
            status_code=429,
        ) from last_error

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call_with_one_retry(
        self, model: str, system_prompt: str, user_message: str
    ) -> str:
        """
        Attempt a single model.  On 429, sleep Retry-After seconds and try once
        more.  Raises LLMCallError for any failure including a second 429.
        """
        response = self._http_post(model, system_prompt, user_message)

        if response.status_code == 429:
            wait = _parse_retry_after(response)
            logger.warning(
                "429 received — sleeping before retry",
                extra={"model": model, "retry_after_seconds": wait},
            )
            time.sleep(wait)

            # One retry.
            response = self._http_post(model, system_prompt, user_message)

        if response.status_code != 200:
            logger.warning(
                "LLM API returned non-200",
                extra={"model": model, "status_code": response.status_code},
            )
            raise LLMCallError(
                f"LLM API returned HTTP {response.status_code} for model {model!r}: "
                f"{response.text[:200]}",
                status_code=response.status_code,
            )

        return self._extract_content(response, model)

    def _http_post(
        self, model: str, system_prompt: str, user_message: str
    ) -> httpx.Response:
        """
        Perform the raw HTTP POST.  Raises LLMCallError on network/timeout errors.
        """
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # OpenRouter recommends these headers for routing / analytics.
            "HTTP-Referer": "https://github.com/ledgergate-agent",
            "X-Title": "LedgerGate-Agent",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            # temperature=0 for maximum determinism on extraction tasks.
            "temperature": 0,
        }

        logger.info(
            "Calling LLM",
            extra={"model": model, "url": url},
        )

        try:
            return httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMCallError(
                f"LLM request timed out (model={model!r}): {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise LLMCallError(
                f"LLM request failed (model={model!r}): {exc}"
            ) from exc

    def _extract_content(self, response: httpx.Response, model: str) -> str:
        """Parse the 200 response and return the assistant message content."""
        try:
            data = response.json()
            content: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMCallError(
                f"Unexpected LLM response shape (model={model!r}): {exc}. "
                f"Response snippet: {response.text[:200]}"
            ) from exc

        logger.info("LLM call succeeded", extra={"model": model})
        return content
