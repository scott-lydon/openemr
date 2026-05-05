"""ClamAV INSTREAM virus scan.

Every byte that crosses the upload boundary is fed to ``clamd`` over
its Unix socket (or TCP, if configured). A scanner returning ``FOUND``
raises ``UploadVirusError`` and the upload is aborted before the bytes
are written to the FHIR DocumentReference.

Why scan every upload, even a clinically-controlled one:

- The threat model includes a compromised user account uploading a
  malicious PDF disguised as a referral fax. ClamAV catches the well-known
  classes that user-content portals usually catch.
- The eval suite uses an EICAR test signature to confirm the scanner is
  wired and the typed exception is raised.

Why a thin wrapper module:

- The ``clamd`` Python client raises a different exception type on
  network failure than on signature hit. Conflating those two would
  produce misleading 422s when the scanner is actually offline. The
  wrapper distinguishes them and surfaces the correct typed error.
- The unit tests can substitute a deterministic stub ``Scanner`` without
  touching the daemon.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Final, Protocol

from sidecar.ingest.errors import UploadVirusError


# EICAR test string (the standard anti-virus benchmark signature).
# Documented in the EICAR test file specification. Including it as a
# constant lets us assert the scanner detects it without copy-pasting
# the bytes into adversarial fixtures every time.
EICAR_TEST_BYTES: Final[bytes] = (
    b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


class Scanner(Protocol):
    """Protocol for a virus scanner.

    The production implementation is ``ClamdScanner`` (delegates to
    ``clamd``). Tests inject ``StubScanner`` to model both clean and
    infected outcomes deterministically.
    """

    def scan_bytes(self, data: bytes) -> ScanResult:
        ...


@dataclass(frozen=True)
class ScanResult:
    """One scanner verdict.

    ``signature`` carries the matched signature name when ``infected``
    is True; the upload pipeline writes it to the audit log but never
    returns it to the caller.
    """

    infected: bool
    signature: str | None


class ClamdScanner:
    """Production scanner. Talks to ``clamd`` via Unix socket or TCP.

    Connection settings come from environment variables so deployment
    can switch between Unix socket (development) and TCP (staging,
    production) without a code change:

    - ``COPILOT_CLAMD_SOCKET`` — path to the Unix socket.
    - ``COPILOT_CLAMD_HOST`` + ``COPILOT_CLAMD_PORT`` — TCP fallback.

    The connection is cheap (clamd accepts and closes per-scan), so we
    do not pool. If profiling shows scan latency dominated by connect,
    introduce a pool here without changing the call sites.
    """

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        host: str | None = None,
        port: int = 3310,
    ) -> None:
        self._socket_path = socket_path
        self._host = host
        self._port = port

    def scan_bytes(self, data: bytes) -> ScanResult:
        try:
            import clamd  # type: ignore[import-untyped]
        except ImportError as exc:
            raise UploadVirusError(
                "clamd python package is not installed; install the w2_ingest "
                "extra (pip install -e '.[w2_ingest]') and ensure clamd is "
                "running. Underlying ImportError: " + repr(exc)
            ) from exc

        if self._socket_path:
            client = clamd.ClamdUnixSocket(path=self._socket_path)
        elif self._host:
            client = clamd.ClamdNetworkSocket(host=self._host, port=self._port)
        else:
            raise UploadVirusError(
                "No clamd connection configured. Set COPILOT_CLAMD_SOCKET "
                "or COPILOT_CLAMD_HOST."
            )

        try:
            verdict = client.instream(io.BytesIO(data))
        except Exception as exc:  # clamd raises a family of socket errors
            raise UploadVirusError(
                f"clamd unreachable or scan failed: {exc!r}"
            ) from exc

        # clamd returns {"stream": ("OK", None)} or {"stream": ("FOUND", "Eicar-...")}
        stream_verdict = verdict.get("stream", ("ERROR", None))
        if not isinstance(stream_verdict, tuple) or len(stream_verdict) != 2:
            raise UploadVirusError(
                f"clamd returned an unrecognized verdict shape: {verdict!r}"
            )
        status, signature = stream_verdict
        if status == "OK":
            return ScanResult(infected=False, signature=None)
        if status == "FOUND":
            return ScanResult(infected=True, signature=str(signature) if signature else None)
        raise UploadVirusError(
            f"clamd returned status={status!r} signature={signature!r} — "
            "neither OK nor FOUND. Inspect the clamd log on the daemon host."
        )


class StubScanner:
    """Deterministic scanner for unit tests.

    The constructor takes a list of ``(needle_bytes, signature_name)``
    pairs. ``scan_bytes`` reports infected when any needle is a substring
    of the input. The default catches the EICAR string.
    """

    def __init__(
        self,
        infected_needles: list[tuple[bytes, str]] | None = None,
    ) -> None:
        if infected_needles is None:
            infected_needles = [(EICAR_TEST_BYTES, "Eicar-Test-Signature")]
        self._needles = list(infected_needles)

    def scan_bytes(self, data: bytes) -> ScanResult:
        for needle, signature in self._needles:
            if needle in data:
                return ScanResult(infected=True, signature=signature)
        return ScanResult(infected=False, signature=None)


def default_scanner() -> Scanner:
    """Return the production scanner configured from the environment.

    Returns a ``StubScanner`` only when ``COPILOT_VIRUS_SCAN=stub`` is
    set. Defaulting to a real scanner means a missing environment
    variable in production is caught by the very first upload, not
    masked by a permissive fall-through.
    """
    mode = os.environ.get("COPILOT_VIRUS_SCAN", "clamd")
    if mode == "stub":
        return StubScanner()
    if mode != "clamd":
        raise UploadVirusError(
            f"COPILOT_VIRUS_SCAN={mode!r}: only 'clamd' or 'stub' are valid."
        )
    return ClamdScanner(
        socket_path=os.environ.get("COPILOT_CLAMD_SOCKET"),
        host=os.environ.get("COPILOT_CLAMD_HOST"),
        port=int(os.environ.get("COPILOT_CLAMD_PORT", "3310")),
    )


def assert_clean(scanner: Scanner, data: bytes) -> None:
    """Scan ``data``; raise ``UploadVirusError`` if the verdict is infected.

    Returns ``None`` on a clean verdict so the caller does not have to
    bind the result to a variable just to discard it.
    """
    result = scanner.scan_bytes(data)
    if result.infected:
        raise UploadVirusError(
            f"clamav signature={result.signature or 'unknown'!r}"
        )


__all__ = [
    "ClamdScanner",
    "EICAR_TEST_BYTES",
    "ScanResult",
    "Scanner",
    "StubScanner",
    "assert_clean",
    "default_scanner",
]
