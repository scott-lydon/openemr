"""Multipurpose Internet Mail Extensions (MIME) sniffing.

We do not trust the client-supplied ``Content-Type`` header — anyone with
a curl prompt can set it to anything. Instead we feed the first bytes
into ``python-magic`` (libmagic), which reads the file's actual magic
bytes and returns its true type.

Why a thin wrapper module:

- The libmagic Python binding is awkward (initialization side effects,
  thread safety caveats). One module owns the dance so the rest of the
  pipeline is unaware.
- The wrapper substitutes a deterministic mock when ``python-magic`` is
  not installed, so unit tests that exercise the upload handler do not
  need libmagic on the host.
"""

from __future__ import annotations

from typing import Final

from sidecar.ingest.errors import UploadMimeError
from sidecar.ingest.types import AcceptedMimeType


# Number of bytes libmagic needs to identify every type we accept. The
# PDF magic ``%PDF-`` is the first 5 bytes; PNG is 8; JPEG is 3; TIFF is
# 4. Reading a fixed prefix lets us sniff streamed uploads without
# buffering the whole body.
SNIFF_PREFIX_BYTES: Final[int] = 4096


def _accepted_values() -> set[str]:
    """Closed set of MIME values the pipeline accepts."""
    return {value.value for value in AcceptedMimeType}


def detect_mime_type(prefix: bytes) -> AcceptedMimeType:
    """Detect the MIME type from a byte prefix.

    Raises ``UploadMimeError`` if the detected type is not in the
    whitelist. The exception's ``detail`` carries the detected MIME so
    a developer reading the trace knows what was rejected without
    re-running the upload.

    The function is pure: same input → same output. Easy to unit test
    with fixture bytes.
    """
    if not prefix:
        raise UploadMimeError("MIME sniffer received zero bytes")

    detected = _detect_with_libmagic(prefix)

    accepted = _accepted_values()
    if detected not in accepted:
        raise UploadMimeError(
            f"detected_mime={detected!r} not in whitelist={sorted(accepted)!r}"
        )
    return AcceptedMimeType(detected)


def _detect_with_libmagic(prefix: bytes) -> str:
    """Call libmagic via ``python-magic``; fall back to a pure-Python
    sniffer when the binding is unavailable.

    The fallback is intentionally narrow: it only knows the four
    whitelist formats. Any unknown bytes get ``application/octet-stream``,
    which the caller will reject. This keeps the unit-test environment
    happy without weakening security in production.
    """
    try:
        import magic  # type: ignore[import-untyped]
    except ImportError:
        return _fallback_sniff(prefix)

    detected = magic.from_buffer(prefix, mime=True)
    if not isinstance(detected, str):
        # python-magic >= 0.4.27 returns ``str``; older returns ``bytes``.
        # Decoding defensively rather than asserting the version because
        # the install matrix on a developer machine is not under our
        # control.
        detected = bytes(detected).decode("ascii", errors="replace")
    return detected


def _fallback_sniff(prefix: bytes) -> str:
    """Pure-Python identifier for the four whitelist types.

    Documented magic bytes:

    - PDF:  ``%PDF-`` (RFC 8118 + ISO 32000-1).
    - PNG:  ``\\x89PNG\\r\\n\\x1a\\n`` (RFC 2083).
    - JPEG: ``\\xff\\xd8\\xff`` (JFIF, JPEG File Interchange Format).
    - TIFF: ``II*\\x00`` (little-endian) or ``MM\\x00*`` (big-endian).
    """
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith(b"II*\x00") or prefix.startswith(b"MM\x00*"):
        return "image/tiff"
    return "application/octet-stream"


__all__ = ["SNIFF_PREFIX_BYTES", "detect_mime_type"]
