"""SMART Backend Services jwt-bearer helpers used by setup-openemr-client.sh.

Three subcommands so the bash wrapper stays declarative:

  generate-keypair --private-out PATH --jwks-out PATH [--kid KID]
      Generate an RSA-2048 keypair if either output is missing. Idempotent:
      both files must already exist as a matched pair, or both are
      regenerated. Private key written PKCS#8 PEM, mode 0600. JWKS
      (JSON Web Key Set per RFC 7517) written as a single-key set with
      use=sig, alg=RS256.

  print-jwks --jwks PATH
      Re-emit the JWKS file's contents on stdout (newline-stripped).
      Used by the bash wrapper to feed --jwks-json @file to the
      Symfony provisioning command.

  verify --client-id ID --private-key PATH --token-url URL [--scope SCOPE]
      Mint a fresh jwt-bearer assertion, post grant_type=client_credentials
      to the token URL, and exit 0 iff OpenEMR returns an access_token.
      On failure, prints HTTP status and the first 300 chars of the body
      to stderr and exits non-zero with an exit code that mirrors the
      bash wrapper's: 0=ok, 3=verification failed, 1=other.

  fhir-query --client-id ID --private-key PATH --token-url URL
             --fhir-base URL --path 'Condition?patient=…' [--scope SCOPE]
      Same handshake as `verify` to obtain an access token, then GET an
      arbitrary FHIR path under --fhir-base with that token. Pretty-prints
      the JSON response to stdout. Use to compare what OpenEMR FHIR
      actually returns vs. what is in the database — answers
      "FHIR-returned-empty vs sidecar-dropped-them" without instrumenting
      the sidecar.

The script is dependency-light by design — only `cryptography` (for
RSA key generation) and `urllib`/`json` from the stdlib. It does NOT
depend on PyJWT so it can run from a fresh interpreter before the
sidecar's editable install completes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key


_RSA_KEY_SIZE_BITS = 2048
_ASSERTION_LIFETIME_SECONDS = 240
_JWT_BEARER_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def _b64url(data: bytes) -> str:
    """Encode bytes as RFC 7515 base64url (no padding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64url(value: int) -> str:
    """Encode an int as the unsigned big-endian byte string base64url'd."""
    length = max(1, (value.bit_length() + 7) // 8)
    return _b64url(value.to_bytes(length, "big"))


def _public_jwk_from_rsa(public_key: rsa.RSAPublicKey, kid: str) -> dict:
    """Build a JWK (RFC 7517 §4) for an RSA public key.

    alg = RS384 because OpenEMR's JWTClientAuthenticationService uses
    RsaSha384Signer to verify client assertions; an RS256-tagged key
    would be rejected at verification time even though the modulus
    and exponent are correct.
    """
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS384",
        "kid": kid,
        "n": _int_to_b64url(numbers.n),
        "e": _int_to_b64url(numbers.e),
    }


def _generate_keypair(private_out: Path, jwks_out: Path, kid: str) -> None:
    private_out.parent.mkdir(parents=True, exist_ok=True)
    jwks_out.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(
        public_exponent=65537, key_size=_RSA_KEY_SIZE_BITS
    )
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Write the private key with an exclusive umask so it never appears
    # group/world-readable even for a microsecond.
    fd = os.open(private_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    # Belt-and-suspenders: re-chmod in case the umask was already strict.
    os.chmod(private_out, stat.S_IRUSR | stat.S_IWUSR)

    jwks = {"keys": [_public_jwk_from_rsa(key.public_key(), kid)]}
    jwks_out.write_text(json.dumps(jwks, indent=2) + "\n", encoding="utf-8")


def cmd_generate_keypair(args: argparse.Namespace) -> int:
    private_out = Path(args.private_out).expanduser()
    jwks_out = Path(args.jwks_out).expanduser()
    kid = args.kid or "clinical-copilot-sidecar"

    if private_out.exists() and jwks_out.exists():
        print(
            f"keypair already present (private={private_out}, "
            f"jwks={jwks_out}); leaving in place"
        )
        return 0
    if private_out.exists() or jwks_out.exists():
        # One file but not the other = the pair is broken; regenerate
        # both rather than try to derive the missing half from the
        # other and risk a mismatch.
        print(
            "keypair half-present; regenerating both halves",
            file=sys.stderr,
        )
        for p in (private_out, jwks_out):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    _generate_keypair(private_out, jwks_out, kid)
    print(f"generated {private_out} (mode 0600) and {jwks_out}")
    return 0


def cmd_print_jwks(args: argparse.Namespace) -> int:
    path = Path(args.jwks).expanduser()
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 1
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return 0


def _build_assertion(
    *, client_id: str, private_key_pem: bytes, token_url: str, kid: str
) -> str:
    """Mint a SMART Backend Services jwt-bearer assertion.

    Algorithm RS384 + the matching SHA-384 hash, per OpenEMR's
    JWTClientAuthenticationService (which hard-codes RsaSha384Signer
    for client-assertion verification). The header carries the same
    `kid` that appears in the JWKS so OpenEMR's JsonWebKeySet can
    pick the right key out of a multi-key set.
    """
    now = int(time.time())
    header = {"alg": "RS384", "typ": "JWT", "kid": kid}
    payload = {
        "iss": client_id,
        "sub": client_id,
        "aud": token_url,
        "exp": now + _ASSERTION_LIFETIME_SECONDS,
        "iat": now,
        "jti": secrets.token_urlsafe(16),
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode("ascii"))
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode("ascii"))
    signing_input = f"{h}.{p}".encode("ascii")
    key = load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(
            f"expected an RSA private key, got {type(key).__name__}; "
            "regenerate with scripts/_openemr_jwt.py generate-keypair"
        )
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA384())
    return f"{h}.{p}.{_b64url(sig)}"


def _fetch_access_token(
    *,
    client_id: str,
    private_key_pem: bytes,
    token_url: str,
    scope: str,
    kid: str,
    insecure: bool,
) -> tuple[int, str]:
    """Mint an assertion and POST to /token. Returns (http_status, body)."""
    assertion = _build_assertion(
        client_id=client_id,
        private_key_pem=private_key_pem,
        token_url=token_url,
        kid=kid,
    )
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": scope,
            "client_assertion_type": _JWT_BEARER_ASSERTION_TYPE,
            "client_assertion": assertion,
        }
    ).encode("ascii")
    req = urllib.request.Request(
        url=token_url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    import ssl

    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, body_text


def cmd_fhir_query(args: argparse.Namespace) -> int:
    """Mint an access token, GET a FHIR path with it, print the JSON."""
    private_path = Path(args.private_key).expanduser()
    if not private_path.exists():
        print(f"ERROR: private key not found at {private_path}", file=sys.stderr)
        return 1

    insecure = bool(args.insecure)
    status, body = _fetch_access_token(
        client_id=args.client_id,
        private_key_pem=private_path.read_bytes(),
        token_url=args.token_url,
        scope=args.scope,
        kid=args.kid,
        insecure=insecure,
    )
    if status != 200:
        print(f"ERROR: /token returned HTTP {status}: {body[:300]}", file=sys.stderr)
        return 3
    try:
        access_token = json.loads(body)["access_token"]
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"ERROR: could not parse access_token from /token: {exc}", file=sys.stderr)
        print(body[:300], file=sys.stderr)
        return 3

    fhir_url = args.fhir_base.rstrip("/") + "/" + args.path.lstrip("/")
    req = urllib.request.Request(
        url=fhir_url,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/fhir+json",
        },
    )
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(data)
                print(json.dumps(parsed, indent=2))
                # Print a tiny summary to stderr so it does not pollute stdout
                # if the user is piping the JSON elsewhere.
                if isinstance(parsed, dict) and parsed.get("resourceType") == "Bundle":
                    total = parsed.get("total")
                    entries = len(parsed.get("entry") or [])
                    print(
                        f"-- Bundle: total={total}, entries={entries}, url={fhir_url}",
                        file=sys.stderr,
                    )
            except json.JSONDecodeError:
                print(data)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        print(
            f"ERROR: {fhir_url} returned HTTP {exc.code}; body: {body_text[:300]}",
            file=sys.stderr,
        )
        return 3
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    private_path = Path(args.private_key).expanduser()
    if not private_path.exists():
        print(
            f"ERROR: private key not found at {private_path}",
            file=sys.stderr,
        )
        return 1
    pem = private_path.read_bytes()
    try:
        assertion = _build_assertion(
            client_id=args.client_id,
            private_key_pem=pem,
            token_url=args.token_url,
            kid=args.kid,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: failed to build jwt-bearer assertion: {exc}", file=sys.stderr)
        return 1

    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": args.scope,
            "client_assertion_type": _JWT_BEARER_ASSERTION_TYPE,
            "client_assertion": assertion,
        }
    ).encode("ascii")
    req = urllib.request.Request(
        url=args.token_url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    # OpenEMR development-easy uses a self-signed cert on :9300; the
    # bash wrapper points us at HTTP :8300 by default so this fallback
    # rarely matters, but we honour --insecure for HTTPS-only setups.
    import ssl

    ctx = ssl.create_default_context()
    if args.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            status = resp.status
            data = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        data = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
    except (urllib.error.URLError, TimeoutError) as exc:
        print(
            f"ERROR: could not reach {args.token_url}: {exc}",
            file=sys.stderr,
        )
        return 3

    if status == 200:
        try:
            parsed = json.loads(data)
            if isinstance(parsed.get("access_token"), str) and parsed["access_token"]:
                print(
                    f"OK: OpenEMR issued an access_token "
                    f"(expires_in={parsed.get('expires_in')}, "
                    f"scope={parsed.get('scope', '')[:80]})"
                )
                return 0
        except json.JSONDecodeError:
            pass
        print(
            f"ERROR: HTTP 200 from {args.token_url} but body was not a "
            f"valid token response: {data[:300]}",
            file=sys.stderr,
        )
        return 3
    print(
        f"ERROR: {args.token_url} returned HTTP {status}; "
        f"body (first 300 chars): {data[:300]}",
        file=sys.stderr,
    )
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="_openemr_jwt.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser(
        "generate-keypair",
        help="Idempotently generate the RSA-2048 keypair + JWKS pair",
    )
    g.add_argument("--private-out", required=True)
    g.add_argument("--jwks-out", required=True)
    g.add_argument("--kid", default=None)
    g.set_defaults(func=cmd_generate_keypair)

    p = sub.add_parser("print-jwks", help="Cat the JWKS file to stdout")
    p.add_argument("--jwks", required=True)
    p.set_defaults(func=cmd_print_jwks)

    v = sub.add_parser(
        "verify",
        help="POST a real jwt-bearer assertion to /token, exit 0 on access_token",
    )
    v.add_argument("--client-id", required=True)
    v.add_argument("--private-key", required=True)
    v.add_argument("--token-url", required=True)
    v.add_argument("--scope", default="system/Patient.read")
    v.add_argument("--kid", default="clinical-copilot-sidecar",
                   help="JWT kid header (must match the JWKS key id)")
    v.add_argument("--insecure", action="store_true", help="Skip TLS verification")
    v.set_defaults(func=cmd_verify)

    f = sub.add_parser(
        "fhir-query",
        help="GET an arbitrary FHIR path with a freshly-minted access token",
    )
    f.add_argument("--client-id", required=True)
    f.add_argument("--private-key", required=True)
    f.add_argument("--token-url", required=True)
    f.add_argument("--fhir-base", required=True,
                   help="FHIR base URL, e.g. https://localhost:9300/apis/default/fhir")
    f.add_argument("--path", required=True,
                   help="FHIR path beneath --fhir-base, e.g. 'Condition?patient=<uuid>'")
    f.add_argument("--scope", default="system/Patient.read system/Condition.read system/MedicationRequest.read system/AllergyIntolerance.read system/Observation.read system/Encounter.read system/Procedure.read system/DocumentReference.read")
    f.add_argument("--kid", default="clinical-copilot-sidecar")
    f.add_argument("--insecure", action="store_true")
    f.set_defaults(func=cmd_fhir_query)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
