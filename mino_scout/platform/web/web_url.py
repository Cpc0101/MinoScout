"""Strict browser URL and origin helpers shared by auth and execution."""
from __future__ import annotations

from urllib.parse import urljoin, urlsplit, urlunsplit


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _validate_text(value: str, *, code: str = "web_uri_invalid") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(code)
    if "\\" in value or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(code)
    return value


def parse_http_url(value: str, *, code: str = "web_uri_invalid"):
    """Parse an HTTP(S) URL without user info; malformed ports are rejected too."""
    text = _validate_text(str(value or ""), code=code)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError(code) from None
    if parsed.scheme not in _DEFAULT_PORTS or not parsed.hostname:
        raise ValueError(code)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(code)
    # A missing scheme separator or an authority hidden in a path is not a URL.
    if not parsed.netloc or parsed.netloc.startswith(":") or parsed.hostname.endswith("."):
        raise ValueError(code)
    return parsed, port


def origin(url: str) -> str:
    """Canonical HTTP origin: scheme + lowercase host + effective port."""
    parsed, port = parse_http_url(url, code="web_auth_origin_invalid")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port and port != _DEFAULT_PORTS[parsed.scheme] else ""
    return f"{parsed.scheme}://{host}{suffix}"


def validate_page_url(url: str) -> str:
    """Validate a current page URL before comparing or navigating from it."""
    parsed, _ = parse_http_url(url, code="web_uri_invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, parsed.fragment))


def resolve_uri(base: str, uri: str) -> str:
    """Resolve a relative URL within the current HTTP origin."""
    base_v = validate_page_url(base)
    uri_v = _validate_text(str(uri or ""))
    target = urljoin(base_v, uri_v)
    parse_http_url(target)
    if origin(target) != origin(base_v):
        raise ValueError("web_uri_origin_mismatch")
    return target


def same_entry_url(actual: str, expected: str) -> bool:
    left, _ = parse_http_url(actual)
    right, _ = parse_http_url(expected)
    return origin(actual) == origin(expected) and (
        left.path.rstrip("/"), left.query, left.fragment
    ) == (right.path.rstrip("/"), right.query, right.fragment)


def normalize_goto_url(value: str) -> str:
    """Normalize a user-entered HTTP(S) target while retaining strict validation."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        lowered = text.lower()
        if (
            lowered.startswith("localhost")
            or lowered.startswith("127.0.0.1")
            or "." in text.split("/", 1)[0].split(":", 1)[0]
            or text.startswith("[")
        ):
            text = "https://" + text
    try:
        parsed, _ = parse_http_url(text)
    except ValueError:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, parsed.fragment))
