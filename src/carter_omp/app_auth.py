"""GitHub App authentication for the credential sidecar.

The github-proxy is the ONLY process that ever sees the App private key or
any GitHub token. It mints short-lived installation tokens (cached until
shortly before expiry), scoped to the target repository where practical,
and never passes them to the orchestrator or the OMP subprocess.

Pure-stdlib JWT (RS256) so the proxy needs no new dependency: the PEM is
parsed minimally (PKCS#1 / PKCS#8) and RSA signing is done with raw
modular exponentiation via ``pow``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Installation tokens are cached until this far before real expiry, so a
# slow clock or a long request never uses a dead token.
TOKEN_SKEW_SECONDS = 60
APP_JWT_TTL_SECONDS = 540
INSTALLATION_TOKEN_TTL_SECONDS = 3600


# trace:exempt reason=trivial-base64-codec-helpers
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _read_der_length(buf: bytes, offset: int) -> tuple[int, int]:
    first = buf[offset]
    if first < 0x80:
        return first, offset + 1
    count = first & 0x7F
    length = int.from_bytes(buf[offset + 1 : offset + 1 + count], "big")
    return length, offset + 1 + count


def _read_tlv(buf: bytes, offset: int) -> tuple[int, bytes, int]:
    tag = buf[offset]
    length, offset = _read_der_length(buf, offset + 1)
    return tag, buf[offset : offset + length], offset + length


def _parse_rsa_private_key(pem: str) -> tuple[int, int, int]:
    """Return ``(n, e, d)`` from a PKCS#1 or PKCS#8 RSA PEM string."""
    body = "".join(line.strip() for line in pem.splitlines() if line.strip() and "-----" not in line)
    der = base64.b64decode(body)
    _, content, _ = _read_tlv(der, 0)
    # PKCS#8 wraps PKCS#1: SEQUENCE { version, algId, OCTET STRING }.
    # If the content parses as version=0 + algId SEQUENCE + OCTET STRING,
    # unwrap the inner PKCS#1; otherwise treat content as raw PKCS#1.
    try:
        _, version, off = _read_tlv(content, 0)
        _, _, off = _read_tlv(content, off)
        octet_tag, octet_content, _ = _read_tlv(content, off)
        if version == b"\x00" and octet_tag == 0x04:
            _, inner, _ = _read_tlv(octet_content, 0)
            content = inner
    except (ValueError, IndexError):
        pass
    ints: list[int] = []
    offset = 0
    while offset < len(content):
        int_tag, int_content, offset = _read_tlv(content, offset)
        if int_tag != 0x02:
            raise ValueError("expected INTEGER in RSA private key")
        ints.append(int.from_bytes(int_content or b"\x00", "big"))
    # PKCS#1: version, n, e, d, p, q, dp, dq, qinv.
    if len(ints) >= 4 and ints[0] == 0:
        return ints[1], ints[2], ints[3]
    raise ValueError("could not parse RSA private key (need PKCS#1 or PKCS#8)")


# trace:v1 id=impl.github-app-auth work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
def mint_app_jwt(*, app_id: str, private_key_pem: str, now: float | None = None) -> str:
    now_int = int(now if now is not None else time.time())
    n, e, d = _parse_rsa_private_key(private_key_pem)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"iat": now_int - 60, "exp": now_int + APP_JWT_TTL_SECONDS, "iss": app_id}).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    digest = hashlib.sha256(signing_input).digest()
    # PKCS#1 v1.5 padding for SHA-256 DigestInfo.
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    key_len = (n.bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (key_len - len(digest_info_prefix) - len(digest) - 3) + b"\x00"
    em += digest_info_prefix + digest
    sig_int = pow(int.from_bytes(em, "big"), d, n)
    signature = sig_int.to_bytes(key_len, "big")
    return f"{header}.{payload}.{_b64url(signature)}"


@dataclass
class CachedToken:
    token: str
    expires_at: float


class AppTokenProvider:
    """Mints and caches per-repository GitHub App installation tokens.

    ``transport`` is injectable for tests. The provider never logs or
    returns the private key; only short-lived tokens leave this object,
    and only into the proxy's in-memory ``GitHubClient`` instances.
    """

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._app_id = app_id
        self._key_pem = private_key_pem
        self._transport = transport
        self._lock = threading.RLock()
        self._cache: dict[str, CachedToken] = {}

    def _request_token(self, *, installation_id: int, repositories: list[str] | None) -> tuple[str, float]:
        jwt = mint_app_jwt(app_id=self._app_id, private_key_pem=self._key_pem)
        body: dict[str, Any] = {}
        if repositories:
            body["repositories"] = repositories
        with httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "carter_omp/0.1",
            },
            transport=self._transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
        ) as client:
            resp = client.post(f"/app/installations/{installation_id}/access_tokens", json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub App token mint failed: {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        token = str(data["token"])
        expires_at = time.time() + INSTALLATION_TOKEN_TTL_SECONDS
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                expires_at = (
                    __import__("datetime").datetime.strptime(str(data.get("expires_at", "")), fmt).timestamp()
                    if data.get("expires_at")
                    else expires_at
                )
                break
            except (ValueError, TypeError):
                continue
        return token, expires_at

    def token_for_repo(self, *, installation_id: int, repo: str) -> str:
        """Return a cached installation token scoped to ``repo`` if possible."""
        name = repo.split("/")[-1]
        cache_key = f"{installation_id}:{repo.lower()}"
        now = time.time()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and now < cached.expires_at - TOKEN_SKEW_SECONDS:
                return cached.token
        try:
            token, expires_at = self._request_token(installation_id=installation_id, repositories=[name])
        except RuntimeError as exc:
            # Scoped mint can fail when the installation covers few repos;
            # fall back once to an unscoped token rather than failing closed
            # on a naming technicality. The proxy allowlist still applies.
            log.warning("scoped token mint failed, retrying unscoped", extra={"repo": repo, "err": str(exc)[:200]})
            token, expires_at = self._request_token(installation_id=installation_id, repositories=None)
        with self._lock:
            self._cache[cache_key] = CachedToken(token=token, expires_at=expires_at - TOKEN_SKEW_SECONDS)
        return token

    def token_unscoped(self, *, installation_id: int) -> str:
        now = time.time()
        cache_key = f"{installation_id}:*"
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and now < cached.expires_at - TOKEN_SKEW_SECONDS:
                return cached.token
        token, expires_at = self._request_token(installation_id=installation_id, repositories=None)
        with self._lock:
            self._cache[cache_key] = CachedToken(token=token, expires_at=expires_at - TOKEN_SKEW_SECONDS)
        return token


__all__ = [
    "TOKEN_SKEW_SECONDS",
    "APP_JWT_TTL_SECONDS",
    "CachedToken",
    "AppTokenProvider",
    "mint_app_jwt",
]
