"""Unit tests for app/keepa/client.py, fully mocked via respx.

No real network calls to api.keepa.com happen here -- the real keys are
precious/rate-limited, so every test below intercepts httpx traffic.
"""
import httpx
import pytest
import respx

from app.keepa.client import (
    KeepaClient,
    KeepaKeysExhaustedError,
    KeepaRateLimitError,
)


def _product_response(asins: list[str]) -> dict:
    return {
        "tokensLeft": 100,
        "products": [{"asin": a, "title": f"Product {a}"} for a in asins],
    }


@pytest.mark.asyncio
@respx.mock
async def test_get_products_batches_over_100_asins():
    """>100 ASINs must trigger multiple sequential /product requests,
    merged into a single `products` array, in a single call."""
    asins = [f"B{i:09d}" for i in range(150)]  # 150 ASINs -> 2 batches (100 + 50)

    route = respx.get("https://api.keepa.com/product")

    call_count = {"n": 0}

    def _responder(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        requested_asins = request.url.params["asin"].split(",")
        return httpx.Response(200, json=_product_response(requested_asins))

    route.mock(side_effect=_responder)

    client = KeepaClient(api_keys=["key-a"])
    result = await client.get_products(asins=asins)

    assert call_count["n"] == 2
    assert len(result["products"]) == 150
    returned_asins = {p["asin"] for p in result["products"]}
    assert returned_asins == set(asins)

    # Verify batch sizes were <=100 each.
    batch_sizes = []
    for call in route.calls:
        requested = call.request.url.params["asin"].split(",")
        batch_sizes.append(len(requested))
    assert batch_sizes == [100, 50]


@pytest.mark.asyncio
@respx.mock
async def test_get_products_single_batch_for_small_list():
    asins = ["B000000001", "B000000002"]
    route = respx.get("https://api.keepa.com/product").mock(
        return_value=httpx.Response(200, json=_product_response(asins))
    )

    client = KeepaClient(api_keys=["key-a"])
    result = await client.get_products(asins=asins)

    assert route.call_count == 1
    assert len(result["products"]) == 2


@pytest.mark.asyncio
@respx.mock
async def test_get_products_by_code_uses_code_param_not_asin():
    route = respx.get("https://api.keepa.com/product").mock(
        return_value=httpx.Response(200, json=_product_response(["B0000021VO"]))
    )

    client = KeepaClient(api_keys=["key-a"])
    result = await client.get_products(code="070537500052")

    assert route.call_count == 1
    sent_params = route.calls[0].request.url.params
    assert sent_params["code"] == "070537500052"
    assert "asin" not in sent_params
    assert len(result["products"]) == 1


@pytest.mark.asyncio
async def test_get_products_rejects_both_asins_and_code():
    client = KeepaClient(api_keys=["key-a"])
    with pytest.raises(ValueError):
        await client.get_products(asins=["B01"], code="12345")


@pytest.mark.asyncio
@respx.mock
async def test_402_triggers_key_rotation_to_second_key():
    """First key returns 402 (tokens exhausted); client should rotate to
    the second key and succeed with it, without raising."""
    route = respx.get("https://api.keepa.com/product")

    seen_keys = []

    def _responder(request: httpx.Request) -> httpx.Response:
        key = request.url.params["key"]
        seen_keys.append(key)
        if key == "key-a":
            return httpx.Response(402, json={"error": "out of tokens"})
        return httpx.Response(200, json=_product_response(["B0000021VO"]))

    route.mock(side_effect=_responder)

    client = KeepaClient(api_keys=["key-a", "key-b"])
    result = await client.get_products(asins=["B0000021VO"])

    assert seen_keys == ["key-a", "key-b"]
    assert len(result["products"]) == 1


@pytest.mark.asyncio
@respx.mock
async def test_all_keys_exhausted_raises():
    respx.get("https://api.keepa.com/product").mock(
        return_value=httpx.Response(402, json={"error": "out of tokens"})
    )

    client = KeepaClient(api_keys=["key-a", "key-b"])
    with pytest.raises(KeepaKeysExhaustedError):
        await client.get_products(asins=["B0000021VO"])


@pytest.mark.asyncio
@respx.mock
async def test_429_triggers_retry_then_succeeds():
    """A 429 followed by a 200 on retry should succeed without raising,
    and without needing to rotate keys."""
    route = respx.get("https://api.keepa.com/product")
    call_count = {"n": 0}

    def _responder(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=_product_response(["B0000021VO"]))

    route.mock(side_effect=_responder)

    client = KeepaClient(api_keys=["key-a"])
    # Patch sleep to keep the test fast.
    import app.keepa.client as client_module

    async def _no_sleep(_seconds):
        return None

    original_sleep = client_module.asyncio.sleep
    client_module.asyncio.sleep = _no_sleep
    try:
        result = await client.get_products(asins=["B0000021VO"])
    finally:
        client_module.asyncio.sleep = original_sleep

    assert call_count["n"] == 2
    assert len(result["products"]) == 1


@pytest.mark.asyncio
@respx.mock
async def test_429_exhausts_retries_and_raises():
    respx.get("https://api.keepa.com/product").mock(
        return_value=httpx.Response(429, json={"error": "rate limited"})
    )

    client = KeepaClient(api_keys=["key-a"])

    import app.keepa.client as client_module

    async def _no_sleep(_seconds):
        return None

    original_sleep = client_module.asyncio.sleep
    client_module.asyncio.sleep = _no_sleep
    try:
        with pytest.raises(KeepaRateLimitError):
            await client.get_products(asins=["B0000021VO"])
    finally:
        client_module.asyncio.sleep = original_sleep


@pytest.mark.asyncio
@respx.mock
async def test_get_token_status_hits_free_endpoint():
    respx.get("https://api.keepa.com/token").mock(
        return_value=httpx.Response(
            200, json={"tokensLeft": 300, "refillRate": 5, "refillIn": 35000}
        )
    )

    client = KeepaClient(api_keys=["key-a"])
    status = await client.get_token_status()

    assert status["tokensLeft"] == 300
    assert status["refillRate"] == 5
