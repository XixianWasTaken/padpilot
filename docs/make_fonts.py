"""Build the web fonts in padpilot/fonts/ that the tutorial and manual pages use, so the pages
load nothing from the internet.

    python3 docs/make_fonts.py SRC_DIR

SRC_DIR holds the OFL fonts from https://github.com/google/fonts/tree/main/ofl:
PressStart2P-Regular.ttf, VT323-Regular.ttf and ZCOOLKuaiLe-Regular.ttf (any subfolders).
Needs fonttools and brotli (pip install fonttools brotli).

The two Latin pixel fonts are kept whole. ZCOOL KuaiLe is cut down to the characters the pages
contain, so run this again after adding Chinese text to a page; a character it lacks falls back
to the system font.
"""
import re
import sys
from pathlib import Path

from fontTools import subset

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "padpilot" / "fonts"
PAGES = [ROOT / "padpilot" / "tutorial.html", ROOT / "padpilot" / "manual.html"]
FONTS = {  # source file -> (output name, characters: None = all)
    "PressStart2P-Regular.ttf": ("press-start-2p.woff2", None),
    "VT323-Regular.ttf": ("vt323.woff2", None),
    "ZCOOLKuaiLe-Regular.ttf": ("zcool-kuaile.woff2", "pages"),
}


def page_chars() -> str:
    text = "".join(p.read_text(encoding="utf-8") for p in PAGES)
    text = re.sub(r"data:[^\"')\s]+", "", text)  # embedded images are not text
    chars = set(text) | {chr(c) for c in range(0x20, 0x7F)}
    return "".join(sorted(c for c in chars if c.isprintable()))


def main(src: Path) -> int:
    OUT.mkdir(exist_ok=True)
    for name, (out, which) in FONTS.items():
        found = sorted(src.rglob(name))
        if not found:
            print(f"missing {name} under {src}", file=sys.stderr)
            return 1
        opts = subset.Options()
        opts.flavor = "woff2"
        opts.layout_features = ["*"]
        opts.name_IDs = ["*"]  # keep the copyright and licence names (OFL)
        opts.notdef_outline = True
        font = subset.load_font(str(found[0]), opts)
        sub = subset.Subsetter(opts)
        if which is None:
            sub.populate(unicodes=font.getBestCmap().keys())
        else:
            sub.populate(text=page_chars())
        sub.subset(font)
        subset.save_font(font, str(OUT / out), opts)
        print(f"{out}: {(OUT / out).stat().st_size // 1024} KiB")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
