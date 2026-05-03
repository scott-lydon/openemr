"""Unit tests for the OpenEMR SMART Backend Services token cache.

Covers the jwt-bearer assertion path: configuration validation, JWT
header/claims construction, and signature round-trip against the
generated public key. The end-to-end /token POST is not exercised
here (no live OpenEMR in the unit-test sandbox) — that path is
covered by clinical-copilot/scripts/setup-openemr-client.sh's
verify step against a real OpenEMR.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sidecar.openemr_oauth import (
    OpenEMRConfigurationError,
    OpenEMRTokenCache,
)


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _make_key_file(tmp_path: Path) -> tuple[Path, rsa.RSAPublicKey]:
    """Generate an RSA-2048 keypair and write the private half to PEM."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "key.pem"
    p.write_bytes(pem)
    p.chmod(0o600)
    return p, key.public_key()


class _FakeSettings:
    """Minimal stand-in for sidecar.config.Settings."""

    def __init__(
        self,
        *,
        client_id: str = "test-client-id",
        oauth_base: str = "http://localhost:8300/oauth2/default",
        private_key_path: str | None = None,
        verify_ssl: bool = False,
    ) -> None:
        self.openemr_client_id = client_id
        self.openemr_oauth_base = oauth_base
        self.openemr_private_key_path = private_key_path
        self.fhir_verify_ssl = verify_ssl


def test_token_cache_construction_with_valid_keypair(tmp_path: Path) -> None:
    key_path, _ = _make_key_file(tmp_path)
    cache = OpenEMRTokenCache(_FakeSettings(private_key_path=str(key_path)))
    # Constructor must read the PEM and stash it for later signing.
    assert cache._private_key_pem.startswith(b"-----BEGIN")


def test_token_cache_rejects_missing_client_id(tmp_path: Path) -> None:
    key_path, _ = _make_key_file(tmp_path)
    with pytest.raises(OpenEMRConfigurationError, match="CLIENT_ID is empty"):
        OpenEMRTokenCache(
            _FakeSettings(client_id="", private_key_path=str(key_path))
        )


def test_token_cache_rejects_missing_private_key_path() -> None:
    with pytest.raises(OpenEMRConfigurationError, match="PRIVATE_KEY_PATH is empty"):
        OpenEMRTokenCache(_FakeSettings(private_key_path=None))


def test_token_cache_rejects_nonexistent_key_file(tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist.pem"
    with pytest.raises(OpenEMRConfigurationError, match="no file is there"):
        OpenEMRTokenCache(_FakeSettings(private_key_path=str(bogus)))


def test_token_cache_rejects_non_pem_file(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pem"
    junk.write_text("definitely not a pem private key\n")
    with pytest.raises(OpenEMRConfigurationError, match="does not look like a PEM"):
        OpenEMRTokenCache(_FakeSettings(private_key_path=str(junk)))


def test_assertion_has_required_smart_backend_services_claims(
    tmp_path: Path,
) -> None:
    """RFC 7523 §3 + SMART Backend Services profile: iss, sub, aud, exp, jti."""
    key_path, public_key = _make_key_file(tmp_path)
    cache = OpenEMRTokenCache(
        _FakeSettings(
            client_id="my-client-id",
            private_key_path=str(key_path),
        )
    )
    token = cache._build_client_assertion(audience="https://emr.example/token")
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["typ"] == "JWT"

    claims = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience="https://emr.example/token",
    )
    assert claims["iss"] == "my-client-id"
    assert claims["sub"] == "my-client-id"
    assert claims["aud"] == "https://emr.example/token"
    assert "jti" in claims and claims["jti"]
    # exp must be in the future and within RFC 7523's "short" window.
    now = int(time.time())
    assert claims["exp"] > now
    assert claims["exp"] - now <= 5 * 60


def test_assertion_jti_is_unique_per_call(tmp_path: Path) -> None:
    """A repeated assertion must use a fresh nonce so OpenEMR can detect replay."""
    key_path, _ = _make_key_file(tmp_path)
    cache = OpenEMRTokenCache(
        _FakeSettings(private_key_path=str(key_path)),
    )
    a = cache._build_client_assertion(audience="https://emr/token")
    b = cache._build_client_assertion(audience="https://emr/token")
    payload_a = json.loads(_b64url_decode(a.split(".")[1]))
    payload_b = json.loads(_b64url_decode(b.split(".")[1]))
    assert payload_a["jti"] != payload_b["jti"]


def test_assertion_signature_verifies_with_public_key(tmp_path: Path) -> None:
    """The signature must be a valid RSASSA-PKCS1-v1_5 over header.payload."""
    key_path, public_key = _make_key_file(tmp_path)
    cache = OpenEMRTokenCache(
        _FakeSettings(private_key_path=str(key_path)),
    )
    token = cache._build_client_assertion(audience="https://emr/token")
    # If the signature is wrong, jwt.decode raises InvalidSignatureError.
    jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience="https://emr/token",
    )


def test_assertion_rejected_by_wrong_public_key(tmp_path: Path) -> None:
    key_path, _ = _make_key_file(tmp_path)
    cache = OpenEMRTokenCache(
        _FakeSettings(private_key_path=str(key_path)),
    )
    token = cache._build_client_assertion(audience="https://emr/token")
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            token,
            other_key.public_key(),
            algorithms=["RS256"],
            audience="https://emr/token",
        )
