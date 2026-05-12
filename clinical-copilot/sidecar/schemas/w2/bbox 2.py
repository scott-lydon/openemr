"""Normalized bounding box on a rasterized PDF page."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BoundingBox(BaseModel):
    """Bounding box on a single page of a rasterized document.

    Coordinates are normalized to ``[0.0, 1.0]`` so the schema is
    independent of the rendering DPI. ``x0,y0`` is the top-left corner;
    ``x1,y1`` is the bottom-right. The Vision Language Model returns
    these directly when supported; the citation preview endpoint scales
    them to the rendered image dimensions at display time.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    page: int = Field(
        ge=0,
        description="Zero-indexed page number on the source document.",
    )
    x0: float = Field(ge=0.0, le=1.0, description="Left edge, normalized 0..1.")
    y0: float = Field(ge=0.0, le=1.0, description="Top edge, normalized 0..1.")
    x1: float = Field(ge=0.0, le=1.0, description="Right edge, normalized 0..1.")
    y1: float = Field(ge=0.0, le=1.0, description="Bottom edge, normalized 0..1.")

    @model_validator(mode="after")
    def coords_form_a_real_rectangle(self) -> "BoundingBox":
        """A bbox where ``x1<=x0`` or ``y1<=y0`` is degenerate; reject it.

        Without this check the citation preview renderer happily draws an
        invisible 0-pixel rectangle and the user clicks a citation that
        leads nowhere. Catching at parse time forces the upstream caller
        to fix the source.
        """
        if self.x1 <= self.x0:
            raise ValueError(
                f"BoundingBox is degenerate on page {self.page}: x1={self.x1} <= x0={self.x0}. "
                f"The Vision Language Model returned a zero- or negative-width rectangle. "
                f"Either the model misread the page or the schema mapping is inverted. "
                f"Reject this field and emit an ExtractionWarning instead of persisting it."
            )
        if self.y1 <= self.y0:
            raise ValueError(
                f"BoundingBox is degenerate on page {self.page}: y1={self.y1} <= y0={self.y0}. "
                f"The Vision Language Model returned a zero- or negative-height rectangle. "
                f"Either the model misread the page or the schema mapping is inverted. "
                f"Reject this field and emit an ExtractionWarning instead of persisting it."
            )
        return self
