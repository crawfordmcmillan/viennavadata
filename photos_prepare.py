"""photos_prepare.py — turn original photos into the site's photo treatment.

One-off, run locally when new originals land in photos/originals/ (needs
Pillow). The treatment is a warm ink-on-paper duotone matched to the site
palette: grayscale, gentle contrast, then shadows toward ink #211d13 and
highlights toward paper #f5eedd. Output goes to assets/photos/ (committed);
build.py copies those into site/ and never touches the originals.

Name the originals by subject: caboose, town-hall, maple-avenue,
community-center, teardown, wod-trail (any image extension).
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).parent
ORIGINALS = ROOT / "photos" / "originals"
OUT = ROOT / "assets" / "photos"

INK = (33, 29, 19)
PAPER = (245, 238, 221)
WIDTH = 1600
QUALITY = 80


def duotone_luts():
    return [
        [round(INK[c] + (PAPER[c] - INK[c]) * i / 255) for i in range(256)]
        for c in range(3)
    ]


def main():
    files = [p for p in sorted(ORIGINALS.glob("*"))
             if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    if not files:
        print(f"error   no images in {ORIGINALS}", file=sys.stderr)
        sys.exit(1)
    OUT.mkdir(parents=True, exist_ok=True)
    luts = duotone_luts()
    meta = {}
    for path in files:
        img = ImageOps.exif_transpose(Image.open(path))
        gray = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        if gray.width > WIDTH:
            gray = gray.resize((WIDTH, round(gray.height * WIDTH / gray.width)),
                               Image.LANCZOS)
        toned = Image.merge("RGB", [gray.point(lut) for lut in luts])
        name = path.stem.lower().replace(" ", "-")
        out = OUT / f"{name}.jpg"
        toned.save(out, "JPEG", quality=QUALITY, optimize=True)
        meta[name] = [toned.width, toned.height]
        print(f"toned   {out.name}  {toned.width}x{toned.height}  "
              f"{out.stat().st_size // 1024}KB")
    (OUT / "photos_meta.json").write_text(json.dumps(meta, indent=1),
                                          encoding="utf-8")
    print("done")


if __name__ == "__main__":
    main()
