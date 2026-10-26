"""Render the repository-created .txt invoices as text-based PDFs (no extra dependencies).

Usage: python scripts/make_sample_pdfs.py samples/inv_10_pinecrest.txt [...]

Each form feed (\\f) in the text starts a new PDF page. Text is drawn in 9 pt
Courier so column alignment matches the .txt version.
"""
from pathlib import Path
import sys

PAGE_WIDTH, PAGE_HEIGHT = 612, 792  # US Letter, in points
FONT_SIZE, LEADING, MARGIN = 9, 11, 36


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_to_pdf(text: str) -> bytes:
    pages = [page.rstrip("\n").splitlines() for page in text.split("\f")]
    # Objects 1-3 are the catalog, page tree, and font; each page adds a page and a content object.
    page_ids = [4 + 2 * i for i in range(len(pages))]
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(pages)} >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
    }
    for page_id, lines in zip(page_ids, pages):
        body = "\n".join(f"({_escape(line)}) Tj T*" for line in lines)
        stream = (f"BT /F1 {FONT_SIZE} Tf {LEADING} TL {MARGIN} {PAGE_HEIGHT - MARGIN} Td\n"
                  f"{body}\nET")
        objects[page_id] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>")
        objects[page_id + 1] = (f"<< /Length {len(stream.encode('latin-1'))} >>\n"
                                f"stream\n{stream}\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for obj_id in sorted(objects):
        offsets[obj_id] = len(out)
        out += f"{obj_id} 0 obj\n{objects[obj_id]}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offsets[i]:010d} 00000 n \n" for i in sorted(objects)).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for name in sys.argv[1:]:
        source = Path(name)
        target = source.with_suffix(".pdf")
        target.write_bytes(text_to_pdf(source.read_text(encoding="utf-8")))
        print(f"wrote {target}")
