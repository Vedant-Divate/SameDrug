"""MockTransport tests for the JAP fetcher — zero network calls."""

import json

import httpx
import pytest

from samedrug.pipeline.download import (
    FetchError,
    fetch_jap_products,
    get_guest_token,
)
from samedrug.pipeline.sources import (
    JAP_FETCH_PAYLOAD,
    JAP_PRODUCTS_URL,
    JAP_TOKEN_URL,
)

TOKEN_BODY = {"responseBody": "guest-token-abc", "message": "ok", "responseCode": 200}

ERROR_500_BODY = {"timestamp": "2026-09-06T00:00:00", "status": 500, "error": "expired"}


def _rows(n, start=1):
    return [{"productId": start + i, "drugCode": start + i} for i in range(n)]


def _products_response(rows, total=None):
    total = len(rows) if total is None else total
    return {
        "responseBody": {
            "newProductResponsesList": rows,
            "totalElement": total,
            "isLastPage": True,
        },
        "responseCode": 200,
    }


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_token_extraction_from_response_body_string():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == JAP_TOKEN_URL
        return httpx.Response(200, json=TOKEN_BODY)

    assert get_guest_token(_client(handler)) == "guest-token-abc"


def test_token_missing_field_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"responseBody": {}, "responseCode": 200})

    with pytest.raises(FetchError):
        get_guest_token(_client(handler))


def test_product_post_carries_auth_header_and_exact_payload():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == JAP_TOKEN_URL:
            return httpx.Response(200, json=TOKEN_BODY)
        assert str(request.url) == JAP_PRODUCTS_URL
        seen["authorization"] = request.headers.get("Authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_products_response(_rows(3)))

    data = fetch_jap_products(_client(handler))
    assert seen["authorization"] == "Bearer guest-token-abc"
    assert seen["payload"] == JAP_FETCH_PAYLOAD
    assert len(data["responseBody"]["newProductResponsesList"]) == 3


def test_500_regenerates_token_and_retry_succeeds():
    sequence: list[tuple[str, str]] = []
    tokens = ["token-first", "token-second"]
    posts = {"count": 0}
    auth_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sequence.append((request.method, request.url.path))
        if request.url == JAP_TOKEN_URL:
            return httpx.Response(
                200, json={"responseBody": tokens.pop(0), "responseCode": 200}
            )
        posts["count"] += 1
        auth_headers.append(request.headers.get("Authorization"))
        if posts["count"] == 1:
            return httpx.Response(500, json=ERROR_500_BODY)
        return httpx.Response(200, json=_products_response(_rows(2)))

    data = fetch_jap_products(_client(handler))
    assert sequence == [
        ("GET", "/auth/generateGuestToken"),
        ("POST", "/api/v1/website/getAllProductForWeb"),
        ("GET", "/auth/generateGuestToken"),
        ("POST", "/api/v1/website/getAllProductForWeb"),
    ]
    assert auth_headers == ["Bearer token-first", "Bearer token-second"]
    assert len(data["responseBody"]["newProductResponsesList"]) == 2


def test_500_twice_raises_fetch_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == JAP_TOKEN_URL:
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(500, json=ERROR_500_BODY)

    with pytest.raises(FetchError):
        fetch_jap_products(_client(handler))


def test_partial_pull_alarm():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == JAP_TOKEN_URL:
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(200, json=_products_response(_rows(5), total=2439))

    with pytest.raises(FetchError, match="partial pull"):
        fetch_jap_products(_client(handler))


def test_zero_rows_alarm():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == JAP_TOKEN_URL:
            return httpx.Response(200, json=TOKEN_BODY)
        return httpx.Response(200, json=_products_response([]))

    with pytest.raises(FetchError, match="zero rows"):
        fetch_jap_products(_client(handler))
