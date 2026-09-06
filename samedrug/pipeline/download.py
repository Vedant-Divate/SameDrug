"""Jan Aushadhi fetcher: guest-token bootstrap + one-shot product pull.

Transport contract verified empirically — see docs/sources.md and
samedrug.pipeline.sources. Politeness: exactly 2 HTTP requests per run
(token GET + products POST); one extra POST only on a stale-token retry.

Functions only; no module-level side effects. Importable for tests —
all tests use httpx.MockTransport, never the network.
"""

import httpx

from samedrug.pipeline.sources import (
    JAP_FETCH_PAYLOAD,
    JAP_PRODUCTS_URL,
    JAP_TOKEN_URL,
    USER_AGENT,
)


class FetchError(RuntimeError):
    """Raised when the JAP fetch fails (transport, auth, or drift alarm)."""


def _request_headers(token: str | None = None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_guest_token(client: httpx.Client) -> str:
    """GET the guest token URL and return the token string in responseBody.

    Raises FetchError on non-200 status or a missing/empty token field.
    """
    try:
        response = client.get(JAP_TOKEN_URL, headers=_request_headers())
    except httpx.HTTPError as exc:
        raise FetchError(f"token request failed: {exc}") from exc
    if response.status_code != 200:
        raise FetchError(f"token request returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise FetchError("token response is not valid JSON") from exc
    token = payload.get("responseBody") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise FetchError("token response missing responseBody string")
    return token


def _post_products(client: httpx.Client, token: str) -> httpx.Response:
    try:
        return client.post(
            JAP_PRODUCTS_URL,
            headers=_request_headers(token),
            json=JAP_FETCH_PAYLOAD,
        )
    except httpx.HTTPError as exc:
        raise FetchError(f"products request failed: {exc}") from exc


def _error_excerpt(response: httpx.Response, limit: int = 200) -> str:
    return response.text[:limit]


def _validate_products(data: dict) -> dict:
    """Drift alarm: responseCode==200, non-empty list, totalElement matches."""
    if not isinstance(data, dict) or data.get("responseCode") != 200:
        raise FetchError("products response has responseCode != 200")
    body = data.get("responseBody")
    if not isinstance(body, dict):
        raise FetchError("products response missing responseBody object")
    products = body.get("newProductResponsesList")
    if not isinstance(products, list) or len(products) == 0:
        raise FetchError("products pull returned zero rows")
    total = body.get("totalElement")
    if total != len(products):
        raise FetchError(
            f"partial pull: totalElement={total} but received {len(products)} rows"
        )
    return data


def fetch_jap_products(client: httpx.Client | None = None) -> dict:
    """Fetch the full JAP product list; return the complete response JSON.

    Bootstraps a guest token, POSTs the exact JAP_FETCH_PAYLOAD, and
    validates the pull. An HTTP 500 means a possibly stale token (the
    server returns 500, not 401) → regenerate the token and retry the
    POST once; a second 500 is a hard failure. No retries on 4xx.
    """
    owned = client is None
    if owned:
        client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60)
    assert client is not None  # narrowed for type checkers; owned ⇒ created
    try:
        token = get_guest_token(client)
        response = _post_products(client, token)
        if response.status_code == 500:
            token = get_guest_token(client)
            response = _post_products(client, token)
            if response.status_code == 500:
                raise FetchError(
                    "products request failed twice with HTTP 500 "
                    f"(stale token?): {_error_excerpt(response)}"
                )
        if response.status_code != 200:
            raise FetchError(
                f"products request returned HTTP {response.status_code}: "
                f"{_error_excerpt(response)}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise FetchError("products response is not valid JSON") from exc
        return _validate_products(data)
    finally:
        if owned:
            client.close()
