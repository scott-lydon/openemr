#!/usr/bin/env python3
"""Build a small PDF with an embedded JavaScript ``/OpenAction``.

Used by the Phase 2 manual confirmation in ``W2_VERIFICATION_CHECKLIST.md``
to exercise the PDF sanitizer against a known-bad input. Production
PDFs never carry JavaScript actions, so a real upload of this fixture
exercises the sanitizer's strip path. The sanitized output should NOT
contain ``/JavaScript``, and the upload should succeed (the action is
removed; the document is stored without the dangerous payload).

Usage::

    [Local Mac terminal]
    cd /Users/scottlydon/Desktop/Clutter/iOS/openemr/clinical-copilot
    python scripts/build_malicious_pdf.py /tmp/jspdf.pdf

The script intentionally avoids reportlab (heavy dep) by emitting the
PDF byte stream by hand. It is a fixture builder, not a general PDF
generator.
"""

from __future__ import annotations

import sys
from pathlib import Path


# Same as the test fixture in tests/sidecar/w2/test_upload.py. Kept as a
# constant rather than imported because this script is invoked from a
# clean shell with no PYTHONPATH.
_MALICIOUS_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R "
    b"/OpenAction << /S /JavaScript /JS (app.alert\\('hi'\\);) >> "
    b">>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>\nendobj\n"
    b"xref\n0 4\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000130 00000 n \n"
    b"0000000183 00000 n \n"
    b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n249\n%%EOF\n"
)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: build_malicious_pdf.py <output.pdf>", file=sys.stderr)
        return 2
    out_path = Path(argv[1])
    out_path.write_bytes(_MALICIOUS_PDF_BYTES)
    print(f"wrote {len(_MALICIOUS_PDF_BYTES)} bytes to {out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
