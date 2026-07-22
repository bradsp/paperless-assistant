"""Generate an IMAGE-ONLY PDF (a rasterized fake invoice, no text layer) and
drop it into the Paperless consume folder.

Run INSIDE the Paperless webserver container (it already has Pillow):

    docker compose cp gen-sample.py webserver:/tmp/gen-sample.py
    docker compose exec -T webserver python3 /tmp/gen-sample.py

Why image-only PDF: `pa reocr` downloads the ORIGINAL file. If the original is a
PDF whose pages are raster images (no embedded text), it exercises the fix's
PDF -> PNG rasterization path — exactly the branch that used to 400 when raw PDF
bytes were handed to Ollama's `images` field.
"""
import sys

from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1] if len(sys.argv) > 1 else "/usr/src/paperless/consume/test-invoice.pdf"

W, H = 1000, 1300
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)


def _font(bold: bool, size: int):
    base = "/usr/share/fonts/truetype/dejavu/DejaVuSans"
    path = f"{base}-Bold.ttf" if bold else f"{base}.ttf"
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


title = _font(True, 46)
head = _font(True, 30)
body = _font(False, 28)

lines = [
    (title, "ACME HARDWARE SUPPLY CO."),
    (body, "1420 Industrial Parkway, Springfield"),
    (body, ""),
    (head, "INVOICE  #  A-20260707-0042"),
    (body, "Date: 2026-07-07     Terms: Net 30"),
    (body, "Bill To: Northgate Property Management"),
    (body, ""),
    (body, "Qty  Description                         Amount"),
    (body, "----------------------------------------------"),
    (body, "  4  Galvanized hinge, 3in               $  18.00"),
    (body, "  2  Exterior deadbolt lock              $  47.50"),
    (body, "  1  Box wood screws, #8 x 2in           $   9.25"),
    (body, " 10  LED flood bulb, 65W eq              $  74.90"),
    (body, "----------------------------------------------"),
    (body, "Subtotal                                $ 149.65"),
    (body, "Tax (7%)                                $  10.48"),
    (head, "TOTAL DUE                               $ 160.13"),
    (body, ""),
    (body, "Thank you for your business."),
]

y = 60
for font, text in lines:
    d.text((60, y), text, font=font, fill="black")
    y += font.size + 14

# resolution matters: it sets the PDF page size so downstream rasterization is sane.
img.save(OUT, "PDF", resolution=150.0)
print(f"wrote {OUT}")
