"""In-memory cache of mock-mode uploads.

Why this module exists
----------------------

In mock mode (``COPILOT_ALLOW_MOCK=true``) the document upload route
short-circuits the production pipeline (Postgres queue, ClamAV, FHIR
DocumentReference). The bytes still get pushed into OpenEMR's documents
store via ``cli-store-document.php`` for the visual-demo flow, but the
sidecar itself loses the bytes the moment the upload handler returns.

The Week-2 chat then needs those bytes back: when the user attaches a
PDF to a chat turn, the intake-extractor node has to read the contents
of the document so the synthesized answer reflects what's actually in
the file (rather than a hard-coded placeholder).

Production wires the intake extractor against the FHIR
DocumentReference, which we cannot reach from mock mode. So we keep a
small bounded cache here, populated by the upload handler and consulted
by ``real_intake_extractor`` in ``chat_w2.py``.

Bounded by design
-----------------

The cache is keyed by document_id (``mock-doc-<sha12>``) and capped at
``_MAX_ENTRIES`` items. Oldest entries evict in insertion order. This
prevents an attacker (or a leaky test) from filling RAM by uploading
many large PDFs in a tight loop.

Thread-safety
-------------

FastAPI runs handlers on the same event loop, but we still take a lock
around mutations so a future move to a thread-pool executor does not
silently corrupt the dict.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass


# Cap chosen to be far smaller than the upload size cap so a runaway
# loop can't OOM the sidecar — at 50 entries × 25 MiB each the worst
# case is ~1.25 GiB which is still high but recoverable. Tune via env if
# the demo setup changes.
_MAX_ENTRIES = 50


@dataclass(frozen=True)
class CachedUpload:
    """One mock upload's bytes plus the metadata we need downstream.

    ``mime_hint`` and ``filename`` come straight from the multipart
    form. The intake extractor uses ``mime_hint`` to decide whether to
    bother trying pypdf and falls back to a plain-text claim otherwise.
    """

    body: bytes
    mime_hint: str
    filename: str


_cache: "OrderedDict[str, CachedUpload]" = OrderedDict()
_lock = threading.Lock()


def store(
    *,
    document_id: str,
    body: bytes,
    mime_hint: str,
    filename: str,
) -> None:
    """Insert one upload's bytes into the cache, evicting the oldest if full.

    Idempotent for repeat uploads of the same document (the deterministic
    ``mock-doc-<sha12>`` id collapses duplicates onto the same entry).
    """
    if not document_id:
        raise ValueError(
            "store(document_id=...) was called with an empty id; the "
            "upload handler must always pass the deterministic mock-doc id."
        )
    with _lock:
        if document_id in _cache:
            # Refresh insertion order so a re-uploaded doc doesn't get
            # evicted before docs that haven't been touched recently.
            _cache.move_to_end(document_id)
            return
        _cache[document_id] = CachedUpload(
            body=body, mime_hint=mime_hint, filename=filename
        )
        while len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)


def fetch(document_id: str) -> CachedUpload | None:
    """Return the cached upload for ``document_id`` or ``None`` if absent.

    A miss is normal: e.g. the sidecar restarted between upload and
    chat. The intake extractor must therefore handle ``None`` and fall
    back to a clearly-flagged placeholder claim rather than crashing.
    """
    with _lock:
        return _cache.get(document_id)


def clear() -> None:
    """Empty the cache. Intended for tests only."""
    with _lock:
        _cache.clear()


__all__ = ["CachedUpload", "clear", "fetch", "store"]
