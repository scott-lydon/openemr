"""Citation preview rendering and signed-URL utilities.

The Phase 6 contract: every clinical claim carries a machine-readable
``Citation``. The user interface clicks one and the side panel opens
with the rendered source page plus a translucent orange overlay around
the cited value.

Public surface:

- ``render_bbox_overlay`` — Portable Document Format (PDF) page +
  bounding box → annotated Portable Network Graphics (PNG).
- ``fuzzy_quote_search`` — fallback when the Vision Language Model did
  not return a native bounding box; locates the quote on the page and
  returns its rectangle.
- ``mint_signed_url`` / ``verify_signed_url`` — 5-minute Time To Live
  (TTL) signed URL helpers backing the preview endpoint.
- ``get_citation`` / ``insert_citation`` — Postgres operations against
  the ``citations`` table.
"""

from sidecar.citations.persistence import (
    CitationRow,
    CitationRowNotFound,
    get_citation,
    insert_citation,
)
from sidecar.citations.preview_renderer import (
    PreviewRenderError,
    bbox_to_pixel_rect,
    fuzzy_quote_search,
    render_bbox_overlay,
)
from sidecar.citations.signing import (
    SignedUrlError,
    SignedUrlExpired,
    SignedUrlSignatureInvalid,
    mint_signed_url,
    verify_signed_url,
)

__all__ = [
    "CitationRow",
    "CitationRowNotFound",
    "PreviewRenderError",
    "SignedUrlError",
    "SignedUrlExpired",
    "SignedUrlSignatureInvalid",
    "bbox_to_pixel_rect",
    "fuzzy_quote_search",
    "get_citation",
    "insert_citation",
    "mint_signed_url",
    "render_bbox_overlay",
    "verify_signed_url",
]
