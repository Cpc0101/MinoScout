"""Process-wide environment initialized before Scout starts networking."""
from __future__ import annotations

import os
import sys
from pathlib import Path

TLS_BUNDLE_ENV_KEYS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def _certifi_bundle() -> Path:
    """Return certifi's CA file, with the frozen-install path as a fallback."""
    try:
        import certifi

        candidate = Path(certifi.where()).expanduser()
        if candidate.is_file():
            return candidate
    except ImportError:
        pass

    candidate = Path(sys.executable).resolve().parent / "_internal" / "certifi" / "cacert.pem"
    if candidate.is_file():
        return candidate
    raise RuntimeError("certifi CA bundle is missing")


def configure_tls_environment() -> dict[str, str]:
    """Pin Python HTTPS clients to certifi without overriding a valid custom CA.

    launchd does not inherit an interactive shell's environment. Setting these
    variables in-process makes manual runs and service launches behave the same.
    Existing values are preserved when they point to a readable CA file.
    """
    bundle = _certifi_bundle()
    configured: dict[str, str] = {}
    for key in TLS_BUNDLE_ENV_KEYS:
        current = str(os.environ.get(key) or "").strip()
        current_path = Path(current).expanduser() if current else None
        if current_path is not None and current_path.is_file():
            configured[key] = str(current_path)
            continue
        os.environ[key] = str(bundle)
        configured[key] = str(bundle)
    return configured
