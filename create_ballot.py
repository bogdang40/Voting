"""
Church Deacon Election — Ballot Generator
==========================================
Generates printable ballots with corner alignment markers (for OMRChecker)
and a QR code encoding the ballot number.

Usage:
    python3 create_ballot.py                    # preview ballot (no number)
    python3 create_ballot.py --number 42        # ballot #42
    python3 create_ballot.py --range 1 500      # print run: ballots 1-500
    python3 create_ballot.py --out my.png       # custom filename (single)

Requirements:
    pip install Pillow "qrcode[pil]"
"""

import argparse
from pathlib import Path

# ── Must match template.json exactly ─────────────────────────────────────────
PAGE_W         = 500           # fixed width
BUBBLE_W       = 45
BUBBLE_H       = 45
DA_ORIGIN_X    = 280
FIRST_ORIGIN_Y = 135
BUBBLES_GAP    = 160           # NU at 280+160 = 440
LABELS_GAP     = 100
NU_ORIGIN_X    = DA_ORIGIN_X + BUBBLES_GAP   # 440

# Alignment markers — one in each corner
MARKER_SIZE           = 44    # template units (width = height)
MARKER_MARGIN         = 10    # distance from page edge
MARKER_HALF           = MARKER_SIZE // 2
SHEET_TO_MARKER_RATIO = 11    # sheetWidth / markerWidth in processing space
QR_SIZE_UNITS         = 110   # enlarged QR for better phone-camera decode reliability
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CANDIDATES = [
    "George Filip",
    "Radu Iacoban",
    "Mielus Balmus",
    "Sebi Pitian",
    "Stelica Slatineanu",
    "Ovidiu Hapca",
    "Lucian Roznovan",
]

S = 4   # print scale → 2000 × (PAGE_H*4) px output

BG    = (255, 255, 255)
FG    = (10,  10,  10 )
LGRY  = (200, 200, 200)
MGRY  = (120, 120, 120)
DA_C  = (25,  100,  35)
NU_C  = (170,  20,  20)
GOLD  = (160, 120,   0)
DA_BG = (235, 248, 236)
NU_BG = (253, 235, 235)


def page_height(n: int) -> int:
    """Dynamic page height based on candidate count."""
    return 350 + n * 100


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_font(px: int):
    from PIL import ImageFont
    for path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "Arial.ttf",
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
    ]:
        try:
            return ImageFont.truetype(path, px)
        except (IOError, OSError):
            continue
    # Keep ballot text readable even when OS fonts are missing (e.g. cloud Linux images).
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


def cx_text(draw, text, cx, y, font, color=None):
    if color is None:
        color = FG
    bb = draw.textbbox((0, 0), text, font=font)
    w  = bb[2] - bb[0]
    draw.text((cx - w // 2, y), text, font=font, fill=color)


def draw_bullseye(draw, cx, cy, r, fg=FG, bg=BG):
    """Concentric-circle alignment marker (same pattern as omr_marker.jpg)."""
    rings = [1.00, 0.70, 0.45, 0.20]
    fills = [fg,   bg,   fg,   bg  ]
    for pct, fill in zip(rings, fills):
        rr = int(r * pct)
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=fill)


# ── Generate the reference marker file ───────────────────────────────────────

def save_marker_file(dest: Path, size: int = 120):
    """
    Create omr_marker.jpg — the reference bullseye OMRChecker template-matches
    against in each quadrant of the photographed ballot.
    """
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (size, size), BG)
    d   = ImageDraw.Draw(img)
    draw_bullseye(d, size // 2, size // 2, size // 2 - 2)
    img.save(str(dest), quality=97)


# ── QR code ──────────────────────────────────────────────────────────────────

def make_qr_image(text: str, size_px: int):
    """Return a square QR code PIL image at the requested pixel size."""
    from PIL import Image
    import qrcode
    import qrcode.constants
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return qr_img.resize((size_px, size_px), Image.LANCZOS)


# ── Ballot ────────────────────────────────────────────────────────────────────

def make_ballot(out_path: str, number: int = None, candidates=None,
                qr_prefix: str = "DIACON", save_preview: bool = False):
    """
    Generate a printable ballot PNG.

    Parameters
    ----------
    out_path   : destination .png path
    number     : ballot number embedded in QR code; None = no QR
    candidates : list of candidate display names; defaults to DEFAULT_CANDIDATES
    qr_prefix  : QR data prefix (e.g. "DIACON" or "V001"); joined as "{prefix}-{n:04d}"
    save_preview: also save a {name}_omr_preview.png at template resolution
    """
    from PIL import Image, ImageDraw

    if candidates is None:
        candidates = DEFAULT_CANDIDATES

    n      = len(candidates)
    PAGE_H = page_height(n)

    markers = [
        (MARKER_MARGIN + MARKER_HALF,          MARKER_MARGIN + MARKER_HALF),           # TL
        (PAGE_W - MARKER_MARGIN - MARKER_HALF, MARKER_MARGIN + MARKER_HALF),           # TR
        (MARKER_MARGIN + MARKER_HALF,          PAGE_H - MARKER_MARGIN - MARKER_HALF),  # BL
        (PAGE_W - MARKER_MARGIN - MARKER_HALF, PAGE_H - MARKER_MARGIN - MARKER_HALF),  # BR
    ]

    W = PAGE_W * S
    H = PAGE_H * S
    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    f_title     = load_font(96)
    f_sub       = load_font(52)
    f_instr     = load_font(40)
    f_col_hdr   = load_font(58)
    f_name      = load_font(76)
    f_footer    = load_font(40)
    f_ballot_no = load_font(56)
    f_tiny      = load_font(34)

    # Outer border
    d.rectangle([12, 12, W - 12, H - 12], outline=FG, width=5)

    cx = W // 2

    # ── Corner alignment markers ───────────────────────────────────────────────
    r_print = (MARKER_SIZE // 2) * S
    for (tx, ty) in markers:
        draw_bullseye(d, tx * S, ty * S, r_print)

    # ── Title ──────────────────────────────────────────────────────────────────
    bb = d.textbbox((0, 0), "ALEGERE DE DIACON", font=f_title)
    d.text((cx - (bb[2] - bb[0]) // 2, 44), "ALEGERE DE DIACON",
           font=f_title, fill=FG)
    cx_text(d, "Vot Secret", cx, 168, f_sub, color=MGRY)
    d.line([(60, 252), (W - 60, 252)], fill=GOLD, width=4)
    cx_text(d, "Marcati DA sau NU pentru fiecare candidat propus.",
            cx, 270, f_instr, color=MGRY)

    # ── Column headers ─────────────────────────────────────────────────────────
    hdr_y   = 348
    hdr_h   = 76
    da_cx   = (DA_ORIGIN_X + BUBBLE_W // 2) * S
    nu_cx   = (NU_ORIGIN_X + BUBBLE_W // 2) * S
    cell_hw = 110
    d.rectangle([da_cx - cell_hw, hdr_y, da_cx + cell_hw, hdr_y + hdr_h], fill=DA_BG)
    d.rectangle([nu_cx - cell_hw, hdr_y, nu_cx + cell_hw, hdr_y + hdr_h], fill=NU_BG)
    d.text((60, hdr_y + 10), "CANDIDAT", font=f_col_hdr, fill=MGRY)
    cx_text(d, "DA", da_cx, hdr_y + 10, f_col_hdr, color=DA_C)
    cx_text(d, "NU", nu_cx, hdr_y + 10, f_col_hdr, color=NU_C)
    d.line([(32, hdr_y + hdr_h + 4), (W - 32, hdr_y + hdr_h + 4)], fill=FG, width=3)

    # ── Candidate rows ─────────────────────────────────────────────────────────
    for i, name in enumerate(candidates):
        bubble_top = (FIRST_ORIGIN_Y + i * LABELS_GAP) * S
        bubble_bot = bubble_top + BUBBLE_H * S
        mid_y      = (bubble_top + bubble_bot) // 2

        if i > 0:
            d.line([(32, bubble_top - 14), (W - 32, bubble_top - 14)],
                   fill=LGRY, width=2)

        label = f"{i + 1}.  {name}"
        bb    = d.textbbox((0, 0), label, font=f_name)
        th    = bb[3] - bb[1]
        d.text((60, mid_y - th // 2), label, font=f_name, fill=FG)

        da_x = DA_ORIGIN_X * S
        d.ellipse([da_x, bubble_top, da_x + BUBBLE_W * S, bubble_bot],
                  fill=BG, outline=FG, width=5)

        nu_x = NU_ORIGIN_X * S
        d.ellipse([nu_x, bubble_top, nu_x + BUBBLE_W * S, bubble_bot],
                  fill=BG, outline=FG, width=5)

    # ── Footer line + instruction ──────────────────────────────────────────────
    footer_y = (FIRST_ORIGIN_Y + n * LABELS_GAP) * S + 20
    d.line([(60, footer_y), (W - 60, footer_y)], fill=GOLD, width=3)
    cx_text(d, "Pliati buletinul si puneti-l in urna.",
            cx, footer_y + 18, f_footer, color=MGRY)

    # ── QR code area ──────────────────────────────────────────────────────────
    qr_area_top = footer_y + 130
    d.line([(200, qr_area_top), (W - 200, qr_area_top)], fill=LGRY, width=2)

    if number is not None:
        qr_data  = f"{qr_prefix}-{number:04d}"
        qr_label = f"Buletin  Nr. {number:04d}"

        lbl_y = qr_area_top + 24
        cx_text(d, qr_label, cx, lbl_y, f_ballot_no, color=MGRY)

        qr_px  = QR_SIZE_UNITS * S
        qr_img = make_qr_image(qr_data, qr_px)
        qr_x   = (W - qr_px) // 2
        qr_y   = lbl_y + 84
        img.paste(qr_img, (qr_x, qr_y))

        cx_text(d, qr_data, cx, qr_y + qr_px + 12, f_tiny, color=LGRY)
    else:
        cx_text(d, "[QR — buletin fara numar]", cx, qr_area_top + 70, f_footer, color=LGRY)

    # ── Save ───────────────────────────────────────────────────────────────────
    img.save(out_path)
    num_str = f" #{number:04d}" if number is not None else ""
    print(f"Ballot{num_str:<8} → {out_path}  ({W}×{H} px)")

    if save_preview:
        preview = out_path.replace(".png", "_omr_preview.png")
        img.resize((PAGE_W, PAGE_H), Image.LANCZOS).save(preview)
        print(f"OMR preview     → {preview}  ({PAGE_W}×{PAGE_H} px)")


def main():
    ap = argparse.ArgumentParser(description="Generate church election ballots")
    ap.add_argument("--out",    default="ballot_diacon.png",
                    help="Output filename (single ballot, default: ballot_diacon.png)")
    ap.add_argument("--number", type=int, default=None,
                    help="Ballot number to embed as QR (1-9999)")
    ap.add_argument("--range",  nargs=2, type=int, metavar=("START", "END"),
                    help="Generate a batch of numbered ballots, e.g. --range 1 500")
    args = ap.parse_args()

    # Always refresh the marker reference file
    save_marker_file(Path("inputs/church_vote/omr_marker.jpg"))
    print(f"Marker file     → inputs/church_vote/omr_marker.jpg")

    if args.range:
        start, end = args.range
        out_dir = Path("ballots")
        out_dir.mkdir(exist_ok=True)
        for n in range(start, end + 1):
            make_ballot(str(out_dir / f"ballot_{n:04d}.png"),
                        number=n, save_preview=False)
        print(f"\nGenerated {end - start + 1} ballots in {out_dir}/")
    else:
        make_ballot(args.out, number=args.number, save_preview=True)


if __name__ == "__main__":
    main()
