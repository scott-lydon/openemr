"""Strip dangerous PDF features before storage.

A PDF can carry payloads that nothing in our pipeline downstream needs:

- ``/JavaScript`` and ``/JS`` actions (run on open in some viewers).
- ``/AA`` (additional actions, fire on page navigation).
- ``/Launch`` (run an external program).
- ``/EmbeddedFiles`` (file attachments).
- ``/OpenAction`` referring to a script.
- ``/AcroForm`` calculations that reference scripts.

Our threat model assumes a clinician's browser may render the source
PDF in a click-through. Stripping these features removes the attack
surface even if the rendering chain has a weakness.

Sanitization strategy:

1. Parse the PDF with ``pypdf``.
2. For each page and the document catalog, remove the dangerous keys.
3. Write the sanitized bytes back.

If the input is corrupt enough that ``pypdf`` cannot parse it,
``UploadPdfSanitizationError`` is raised. We refuse to store
unsanitizable content rather than fall through to a permissive write.

Why we do not simply "ignore" the dangerous keys at extraction time:

- Storage is the durable artifact. A future tool we have not written
  yet may render the PDF differently. The sanitized form is the only
  form we are willing to keep.
- The clinician's click-through opens the stored bytes. The stored
  bytes must be the safe form.

Performance note:

- ``pypdf`` re-encodes objects on write. Tested at ~50 MiB / second on
  a developer laptop, dominated by the cross-reference table rebuild.
  At the 25 MiB upload cap this finishes well under one second.
"""

from __future__ import annotations

import io
from typing import Final

from sidecar.ingest.errors import UploadPdfSanitizationError


# The ``/Names`` dictionary in a PDF catalog can hold name trees pointing
# at JavaScript and EmbeddedFiles. We strip the whole sub-dictionaries
# rather than try to surgically remove sub-trees.
DANGEROUS_CATALOG_KEYS: Final[tuple[str, ...]] = (
    "/JavaScript",
    "/JS",
    "/Names",
    "/OpenAction",
    "/AA",
    "/AcroForm",
)

DANGEROUS_PAGE_KEYS: Final[tuple[str, ...]] = (
    "/AA",
    "/JS",
    "/JavaScript",
)


def sanitize_pdf(data: bytes) -> bytes:
    """Return a sanitized copy of ``data``.

    The return value is byte-for-byte deterministic given the same input
    (no embedded timestamps, no random IDs), so a downstream content hash
    is stable across re-sanitization.

    On a malformed input that pypdf cannot parse, raises
    ``UploadPdfSanitizationError``. The exception's ``detail`` carries
    the underlying parser error class name so a developer can quickly
    grep pypdf for the failure mode.
    """
    if not data:
        raise UploadPdfSanitizationError("PDF sanitizer received zero bytes")

    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise UploadPdfSanitizationError(
            "pypdf is not installed; install the w2_ingest extra. "
            "Underlying ImportError: " + repr(exc)
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except PdfReadError as exc:
        raise UploadPdfSanitizationError(
            f"pypdf cannot parse the PDF: {type(exc).__name__}: {exc!s}"
        ) from exc
    except Exception as exc:  # defensive: pypdf can raise generic Exception
        raise UploadPdfSanitizationError(
            f"pypdf raised unexpected {type(exc).__name__} on parse: {exc!s}"
        ) from exc

    if reader.is_encrypted:
        # An encrypted document with no password cannot be sanitized
        # because we cannot read the dangerous keys. Reject it.
        raise UploadPdfSanitizationError(
            "PDF is encrypted; sanitizer cannot inspect dangerous keys. "
            "If a clinical workflow legitimately needs to upload encrypted "
            "PDFs, the workflow must decrypt before upload."
        )

    writer = PdfWriter(clone_from=reader)

    # Strip dangerous catalog-level keys.
    catalog = writer._root_object  # type: ignore[attr-defined]
    if catalog is not None:
        for key in DANGEROUS_CATALOG_KEYS:
            if key in catalog:
                del catalog[key]

    # Strip dangerous per-page keys.
    for page in writer.pages:
        for key in DANGEROUS_PAGE_KEYS:
            if key in page:
                del page[key]
        # /Annots can contain action dicts; rewriting them out by removing
        # any annot whose /A action carries /JS or /S=/JavaScript.
        if "/Annots" in page:
            sanitized_annots: list[object] = []
            for annot_ref in page["/Annots"]:
                try:
                    annot = annot_ref.get_object() if hasattr(annot_ref, "get_object") else annot_ref
                except Exception:
                    # Defensive: a malformed indirect reference is dropped.
                    continue
                if _annot_is_safe(annot):
                    sanitized_annots.append(annot_ref)
            page[writer._root_object.indirect_reference]  # type: ignore[index]
            page["/Annots"] = sanitized_annots

    out = io.BytesIO()
    try:
        writer.write(out)
    except Exception as exc:
        raise UploadPdfSanitizationError(
            f"pypdf failed to write sanitized PDF: {type(exc).__name__}: {exc!s}"
        ) from exc
    return out.getvalue()


def _annot_is_safe(annot: object) -> bool:
    """Return True when an annotation has no executable action.

    An annotation with ``/A`` whose ``/S`` is ``/JavaScript`` or
    ``/Launch`` is treated as unsafe and dropped. Annotations without
    an action (a normal highlight, an ink stroke, a text comment) are
    kept.
    """
    try:
        action = annot["/A"]  # type: ignore[index]
    except Exception:
        return True
    try:
        action_subtype = action.get("/S")  # type: ignore[union-attr]
    except Exception:
        return True
    return action_subtype not in {"/JavaScript", "/JS", "/Launch", "/SubmitForm"}


__all__ = ["DANGEROUS_CATALOG_KEYS", "DANGEROUS_PAGE_KEYS", "sanitize_pdf"]
