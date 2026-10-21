import pytest

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


async def test_security_headers_present(client):
    r = await client.get("/healthz")
    assert r.headers["content-security-policy"].startswith("default-src 'self'; script-src 'self';")
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in r.headers  # http w testach
    assert {name: r.headers.get(name) for name in HEADERS} == HEADERS


async def test_security_headers_on_error_responses(client):
    r = await client.get("/no-such-page")
    assert r.status_code == 404
    assert {name: r.headers.get(name) for name in HEADERS} == HEADERS


async def test_csrf_cookie_flags_over_http(client):
    r = await client.get("/healthz")
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("easytodo_csrf=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie


async def test_https_public_url_adds_hsts_and_secure_csrf_cookie(client_factory):
    c = await client_factory(public_url="https://testserver")
    r = await c.get("/healthz")
    assert r.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert "Secure" in r.headers["set-cookie"]
    assert {name: r.headers.get(name) for name in HEADERS} == HEADERS


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_api_docs_are_disabled(client, path):  # Swagger UI needs a CDN, which the CSP blocks
    assert (await client.get(path)).status_code == 404
