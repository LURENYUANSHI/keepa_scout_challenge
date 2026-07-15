"""Async Keepa API client: `/product` (batched, key-rotating) + `/token`.

Design notes (per KEEPA_QUICKSTART.md + CHALLENGE.md's "自己摸清" list):
  - Batch limit: Keepa's documented cap for `/product?asin=` is 100 ASINs per
    request (comma-separated). We split anything larger into sequential
    chunks of <=100 and merge the `products` arrays client-side.
  - `asin=` and `code=` are mutually exclusive (code is for UPC/EAN lookup)
    — this client refuses to send both.
  - Key rotation: on HTTP 402 (documented as "this key's tokens are used
    up") — or a 200 response whose body indicates the same via a
    `tokensLeft <= 0` (Keepa sometimes returns 200 with an error-ish body
    for token exhaustion rather than a clean 402; we defensively check both)
    — the client rotates to the next configured key and retries the same
    request once per remaining key. Raises KeepaKeysExhaustedError if every
    key is out.
  - 429 (rate limited): exponential backoff (1s/2s/4s), 3 attempts, before
    raising KeepaRateLimitError.
  - gzip: httpx negotiates `Accept-Encoding: gzip` and decodes transparently
    by default — nothing special needed here (unlike raw curl, which needs
    `--compressed` per KEEPA_QUICKSTART.md).
"""
import asyncio
from functools import lru_cache
from typing import Any

import httpx

KEEPA_BASE_URL = "https://api.keepa.com"
MAX_ASINS_PER_REQUEST = 100
RATE_LIMIT_BACKOFFS = (1, 2, 4)  # seconds, one retry per entry


class KeepaError(Exception):
    """Base class for Keepa client errors."""


class KeepaKeysExhaustedError(KeepaError):
    """All configured API keys are out of tokens (or none were configured)."""


class KeepaRateLimitError(KeepaError):
    """Keepa kept returning 429 after all backoff retries were exhausted."""


class KeepaRequestError(KeepaError):
    """Keepa returned a non-retryable error (e.g. 400 bad request)."""


def _looks_token_exhausted(status_code: int, body: dict[str, Any] | None) -> bool:
    if status_code == 402:
        return True
    if body is not None:
        tokens_left = body.get("tokensLeft")
        if tokens_left is not None and tokens_left <= 0:
            return True
    return False


class KeepaClient:
    def __init__(self, api_keys: list[str], *, timeout: float = 30.0):
        if not api_keys:
            raise ValueError("KeepaClient requires at least one API key")
        self._api_keys = list(api_keys)
        self._key_index = 0
        self._timeout = timeout

    @property
    def current_key(self) -> str:
        return self._api_keys[self._key_index]

    def _rotate_key(self) -> bool:
        """Advance to the next key. Returns False if we've cycled through all of them."""
        if self._key_index + 1 >= len(self._api_keys):
            return False
        self._key_index += 1
        return True

    async def _request(
        self, client: httpx.AsyncClient, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """GET `path` with key rotation (402) and backoff retry (429) built in.

        Tries the current key; on token exhaustion, rotates through the
        remaining keys (one attempt each). Within a single key's attempt,
        a 429 gets retried with exponential backoff before moving on.
        """
        keys_tried = 0
        total_keys = len(self._api_keys)

        while keys_tried < total_keys:
            key = self.current_key
            request_params = {**params, "key": key}

            last_rate_limit_response: httpx.Response | None = None
            for attempt, backoff in enumerate((0, *RATE_LIMIT_BACKOFFS)):
                if backoff:
                    await asyncio.sleep(backoff)

                response = await client.get(
                    f"{KEEPA_BASE_URL}{path}",
                    params=request_params,
                    timeout=self._timeout,
                )

                if response.status_code == 429:
                    last_rate_limit_response = response
                    continue

                body: dict[str, Any] | None
                try:
                    body = response.json()
                except ValueError:
                    body = None

                if _looks_token_exhausted(response.status_code, body):
                    break  # fall through to key rotation below

                if response.status_code == 400:
                    raise KeepaRequestError(
                        f"Keepa rejected the request ({path}, params={params!r}): "
                        f"{response.status_code} {response.text}"
                    )

                response.raise_for_status()
                return body if body is not None else {}
            else:
                # Exhausted all backoff attempts on 429 without success.
                raise KeepaRateLimitError(
                    f"Keepa rate-limited {path} after "
                    f"{len(RATE_LIMIT_BACKOFFS) + 1} attempts: "
                    f"{last_rate_limit_response.status_code if last_rate_limit_response else '429'}"
                )

            # Token exhausted on this key -> rotate and retry with the next one.
            keys_tried += 1
            if not self._rotate_key():
                break

        raise KeepaKeysExhaustedError(
            f"All {total_keys} configured Keepa API key(s) are out of tokens "
            f"(path={path})"
        )

    async def get_products(
        self,
        asins: list[str] | None = None,
        code: str | None = None,
        domain: int = 1,
        stats: int = 90,
        buybox: bool = True,
        fbafees: bool = True,
    ) -> dict[str, Any]:
        """GET /product, batching `asins` into chunks of <=100 per request.

        `asins` and `code` are mutually exclusive (Keepa's `asin=` vs
        `code=` params) — passing both raises ValueError.
        """
        if asins and code:
            raise ValueError("get_products: `asins` and `code` are mutually exclusive")
        if not asins and not code:
            raise ValueError("get_products: must pass either `asins` or `code`")

        base_params: dict[str, Any] = {
            "domain": domain,
            "stats": stats,
        }
        if buybox:
            base_params["buybox"] = 1
        if fbafees:
            base_params["fbafees"] = 1

        async with httpx.AsyncClient() as client:
            if code:
                params = {**base_params, "code": code}
                return await self._request(client, "/product", params)

            assert asins is not None
            batches = [
                asins[i : i + MAX_ASINS_PER_REQUEST]
                for i in range(0, len(asins), MAX_ASINS_PER_REQUEST)
            ]

            merged: dict[str, Any] = {"products": []}
            for batch in batches:
                params = {**base_params, "asin": ",".join(batch)}
                result = await self._request(client, "/product", params)
                merged["products"].extend(result.get("products", []))
                # Keep the last response's top-level metadata (tokensLeft etc.)
                # around too, in case callers want to inspect it.
                for key, value in result.items():
                    if key != "products":
                        merged[key] = value

            return merged

    async def get_token_status(self) -> dict[str, Any]:
        """GET /token — free, no token cost. {tokensLeft, refillRate, ...}."""
        async with httpx.AsyncClient() as client:
            return await self._request(client, "/token", {})


@lru_cache
def get_keepa_client() -> "KeepaClient":
    """FastAPI dependency (and general app-wide accessor): a process-wide
    singleton `KeepaClient` built from `settings.KEEPA_API_KEYS`.

    Cached so routers (app/routers/upc.py) and background jobs don't each
    construct their own client -- and, more importantly, don't each start
    key rotation from scratch (`KeepaClient._key_index`) independently.
    Real HTTP calls still go out per-request (each `get_products` call opens
    its own `httpx.AsyncClient`); this only shares the *key rotation state*.

    Tests don't need to override this via `app.dependency_overrides` --
    respx intercepts at the transport layer, so this real (settings-backed)
    client works fine against mocked Keepa responses in tests too.
    """
    from app.config import settings  # local import: keeps config optional at module load

    return KeepaClient(api_keys=settings.keepa_api_keys_list)
