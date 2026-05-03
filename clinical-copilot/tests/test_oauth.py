"""Unit tests for BFF OAuth2 / PKCE / task token utilities."""

from __future__ import annotations

import base64
import hashlib
import time

import pytest

from bff.oauth import (
    make_pkce_pair,
    mint_task_token,
    verify_task_token,
)


def test_pkce_pair_round_trip() -> None:
    verifier, challenge = make_pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected
    # Verifier length must satisfy RFC 7636: 43..128
    assert 43 <= len(verifier) <= 128


def test_task_token_round_trip() -> None:
    key = "this-is-a-32-byte-test-signing-key!!"
    token = mint_task_token(
        signing_key=key,
        user_id="dr.m@example.org",
        patient_id="Patient/87413",
        purposes_of_use=["diagnostic_cross_check"],
        scopes=["patient/Condition.r"],
        lifetime_seconds=300,
    )
    claims = verify_task_token(token, signing_key=key)
    assert claims.user_id == "dr.m@example.org"
    assert claims.patient_id == "Patient/87413"
    assert claims.authorized_purposes == ("diagnostic_cross_check",)
    assert claims.is_purpose_authorized("diagnostic_cross_check")
    assert not claims.is_purpose_authorized("chart_error_scan")
    assert "patient/Condition.r" in claims.scope


def test_task_token_round_trip_multi_purpose() -> None:
    """The launch flow mints with every UI-fanned-out purpose."""
    key = "this-is-a-32-byte-test-signing-key!!"
    token = mint_task_token(
        signing_key=key,
        user_id="dr.m@example.org",
        patient_id="Patient/87413",
        purposes_of_use=[
            "diagnostic_cross_check",
            "chart_error_scan",
            "follow_up_question",
        ],
        scopes=["patient/Condition.r"],
        lifetime_seconds=300,
    )
    claims = verify_task_token(token, signing_key=key)
    assert claims.authorized_purposes == (
        "diagnostic_cross_check",
        "chart_error_scan",
        "follow_up_question",
    )
    assert claims.is_purpose_authorized("diagnostic_cross_check")
    assert claims.is_purpose_authorized("chart_error_scan")
    assert claims.is_purpose_authorized("follow_up_question")
    assert not claims.is_purpose_authorized("billing")


def test_task_token_legacy_string_purpose_accepted() -> None:
    """A token whose purpose_of_use is a plain string (legacy minter)
    still verifies, exposing a single-element authorized_purposes.

    This forward-compatibility path lets in-flight 5-minute tokens
    survive a rolling deploy of the minter+verifier together.
    """
    import base64
    import hashlib
    import hmac
    import json
    import time as _time

    key = "k-for-legacy-string-test"
    now = int(_time.time())
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    ).rstrip(b"=").decode("ascii")
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "iss": "openemr-launch",
            "sub": "u",
            "patient_id": "Patient/1",
            "purpose_of_use": "diagnostic_cross_check",  # legacy string form
            "scope": "",
            "iat": now,
            "nbf": now,
            "exp": now + 60,
            "jti": "legacy",
        }, separators=(",", ":")).encode()
    ).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(
        hmac.new(key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    token = f"{header}.{payload}.{sig}"
    claims = verify_task_token(token, signing_key=key)
    assert claims.authorized_purposes == ("diagnostic_cross_check",)
    assert claims.is_purpose_authorized("diagnostic_cross_check")


def test_mint_rejects_empty_purpose_list() -> None:
    """A token with no authorised purposes is unusable; reject at mint."""
    with pytest.raises(ValueError, match="at least one purpose"):
        mint_task_token(
            signing_key="k",
            user_id="u", patient_id="Patient/1",
            purposes_of_use=[], scopes=[],
        )


def test_mint_rejects_empty_string_purpose() -> None:
    with pytest.raises(ValueError, match=r"purposes_of_use\[0\] is empty"):
        mint_task_token(
            signing_key="k",
            user_id="u", patient_id="Patient/1",
            purposes_of_use=[""], scopes=[],
        )


def test_mint_rejects_non_string_purpose() -> None:
    with pytest.raises(ValueError, match=r"purposes_of_use\[1\] must be a string"):
        mint_task_token(
            signing_key="k",
            user_id="u", patient_id="Patient/1",
            purposes_of_use=["diagnostic_cross_check", 123],  # type: ignore[list-item]
            scopes=[],
        )


def test_task_token_rejects_bad_signature() -> None:
    from sidecar.auth import TaskTokenSignatureError

    token = mint_task_token(
        signing_key="key-A",
        user_id="u", patient_id="Patient/1",
        purposes_of_use=["diagnostic_cross_check"], scopes=[],
    )
    with pytest.raises(TaskTokenSignatureError, match="signature"):
        verify_task_token(token, signing_key="key-B")


def test_task_token_rejects_expired() -> None:
    from sidecar.auth import TaskTokenExpiredError

    token = mint_task_token(
        signing_key="k",
        user_id="u", patient_id="Patient/1",
        purposes_of_use=["diagnostic_cross_check"], scopes=[], lifetime_seconds=0,
    )
    time.sleep(0.1)
    with pytest.raises(TaskTokenExpiredError, match="expired"):
        verify_task_token(token, signing_key="k")


def test_task_token_rejects_default_key() -> None:
    """Refuse to mint or verify with the placeholder signing key."""
    with pytest.raises(ValueError, match="default signing key"):
        mint_task_token(
            signing_key="change-me-to-a-32-byte-hex-string",
            user_id="u", patient_id="Patient/1",
            purposes_of_use=["diagnostic_cross_check"], scopes=[],
        )


def test_task_token_rejects_malformed() -> None:
    from sidecar.auth import TaskTokenMalformedError

    with pytest.raises(TaskTokenMalformedError):
        verify_task_token("not.a.jwt.with.too.many.parts", signing_key="k")
    with pytest.raises(TaskTokenMalformedError):
        verify_task_token("only-one-segment", signing_key="k")
