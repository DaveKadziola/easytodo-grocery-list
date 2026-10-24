"""The security headers every response carries.

Kept apart from ``middleware`` (which sets them on each response) so that ``errors`` can set them
on the page it answers an unhandled error with: that answer is made outside the middleware, and
``middleware`` imports ``errors``.
"""

from easytodo.config import Settings

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def security_headers(settings: Settings) -> dict[str, str]:
    """The headers for every response (HSTS only when the public URL is https)."""
    headers = {
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }
    if settings.cookie_secure:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers
