"""Usage: python3 docs/make_logo.py docs/logo.svg

Generate docs/logo.svg: a 2000s Blingee-style pixel banner. The controller sits in the middle,
the wordmark under it, then the tagline. Text is drawn as pixel rects (no web fonts: GitHub shows
SVGs as images, which cannot load fonts). The controller is docs/controller.png (the padpilot
artwork: cut out, resampled onto its own 139 x 88 pixel grid, then smoothed up to 8x), embedded
as a data URI so the SVG stays one file. All motion is CSS inside the SVG and stops under prefers-reduced-motion."""
import base64, random, sys
from pathlib import Path
OUT = sys.argv[1]
PAD = base64.b64encode((Path(__file__).parent / "controller.png").read_bytes()).decode()
PAD_CELLS = (139, 88)                          # art pixels in docs/controller.png (smoothed, stored 8x)
FONT = {
 "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
 "C": [".####", "#....", "#....", "#....", "#....", "#....", ".####"],
 "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
 "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
 "G": [".####", "#....", "#....", "#..##", "#...#", "#...#", ".###."],
 "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
 "I": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"],
 "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
 "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"],
 "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
 "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
 "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
 "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
 "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
 "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
 "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
 "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"],
 "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
 "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
 "+": [".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."],
 "/": ["....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."],
 " ": ["....."] * 7,
}
BOLD = {                                       # the wordmark: 2-cell stems
 "P": ["#####.", "##..##", "##..##", "#####.", "##....", "##....", "##...."],
 "A": [".####.", "##..##", "##..##", "######", "##..##", "##..##", "##..##"],
 "D": ["#####.", "##..##", "##..##", "##..##", "##..##", "##..##", "#####."],
 "I": ["######", "..##..", "..##..", "..##..", "..##..", "..##..", "######"],
 "L": ["##....", "##....", "##....", "##....", "##....", "##....", "######"],
 "O": [".####.", "##..##", "##..##", "##..##", "##..##", "##..##", ".####."],
 "T": ["######", "..##..", "..##..", "..##..", "..##..", "..##..", "..##.."],
}
def text_rects(s, x0, y0, cell, font=FONT, gap=1):
    out, x = [], x0
    for ch in s:
        g = font[ch]
        n = len(g[0])
        for r, row in enumerate(g):
            c = 0
            while c < n:                      # merge runs into one rect per row
                if row[c] == "#":
                    k = c
                    while k < n and row[k] == "#":
                        k += 1
                    out.append(f'<rect x="{x + c * cell}" y="{y0 + r * cell}" width="{(k - c) * cell}" height="{cell}"/>')
                    c = k
                else:
                    c += 1
        x += (n + gap) * cell
    return "".join(out)
def width(s, cell, font=FONT, gap=1):
    return sum(len(font[ch][0]) + gap for ch in s) * cell - gap * cell

W, H = 1280, 630
MID = W // 2
random.seed(7)

# ---- layout: controller, wordmark, tagline, press start, stacked on the centre line ----
PC = 4                                          # banner units per art pixel
PW, PH = PAD_CELLS[0] * PC, PAD_CELLS[1] * PC
PX, PY = MID - PW // 2, 22
HORIZON = PY + PH + 6                           # the floor grid starts just under the grips
word, c1 = "PADPILOT", 12
w1w = width(word, c1, BOLD)
wx, wy = MID - w1w // 2, HORIZON + 14
w1 = text_rects(word, wx, wy, c1, BOLD)
tag, c3 = "DRIVE CLAUDE CODE + CODEX WITH A PS5 CONTROLLER", 4
tx, ty = MID - width(tag, c3) // 2, wy + 7 * c1 + 30
t = text_rects(tag, tx, ty, c3)
press, c2 = "PRESS START", 5
px_, py_ = MID - width(press, c2) // 2 + 10, ty + 7 * c3 + 26
w2 = text_rects(press, px_, py_, c2)

# ---- decorations ----
SPARK = "M0 -16 L2.4 -2.4 L16 0 L2.4 2.4 L0 16 L-2.4 2.4 L-16 0 L-2.4 -2.4 Z"
STAR5 = "M0 -12 L3.5 -3.8 L12 -3.8 L5.2 1.6 L7.6 10.4 L0 5 L-7.6 10.4 L-5.2 1.6 L-12 -3.8 L-3.5 -3.8 Z"
HEART = [".##.##.", "#######", "#######", ".#####.", "..###..", "...#..."]
def heart(x, y, cell, fill, hi, cls):
    r = "".join(f'<rect x="{x + c * cell}" y="{y + j * cell}" width="{cell}" height="{cell}"/>'
                for j, row in enumerate(HEART) for c, ch in enumerate(row) if ch == "#")
    return (f'<g class="{cls}"><g fill="#0b0620" stroke="#0b0620" stroke-width="{cell}">{r}</g><g fill="{fill}">{r}</g>'
            f'<rect x="{x + cell}" y="{y + cell}" width="{cell}" height="{cell}" fill="{hi}"/></g>')
def spark(x, y, scale, fill, cls):
    return f'<g transform="translate({x} {y}) scale({scale})"><path class="spk {cls}" d="{SPARK}" fill="{fill}"/></g>'
def star5(x, y, scale, cls):
    return (f'<g transform="translate({x} {y}) scale({scale})"><path class="bob {cls}" d="{STAR5}" fill="#ffe34d" stroke="#0b0620" '
            f'stroke-width="2.5" stroke-linejoin="round"/></g>')

stars = []
for _ in range(110):
    x, y = random.randrange(14, W - 14), random.randrange(14, HORIZON)
    col = random.choice(["#ffffff", "#ffffff", "#3ff4ff", "#ff3fd8", "#ffe34d", "#b9a8ff"])
    k = random.choice([2, 2, 3, 4])
    stars.append(f'<rect x="{x}" y="{y}" width="{k}" height="{k}" fill="{col}" class="tw{random.randint(0, 2)}"/>')

# perspective floor: lines fan out from the vanishing point, rows get further apart toward the viewer
floor = []
for k in range(-14, 15):
    floor.append(f'<path d="M{MID + k * 26} {HORIZON} L{MID + k * 150} {H}"/>')
y, step = HORIZON, 7.0
while y < H:
    floor.append(f'<path d="M0 {y:.0f} H{W}"/>')
    y += step
    step *= 1.42

# sparkles on and around the controller (banner coordinates), each with its own phase
on_pad = [(PX + 60, PY + 40, 1.5, "#fff9c4", "a"), (PX + PW - 70, PY + 44, 1.7, "#ffffff", "c"), (PX + PW // 2 + 44, PY + 22, 1.0, "#ffe34d", "b"),
          (PX + 150, PY + 150, .8, "#ffffff", "d"), (PX + PW - 150, PY + 120, .9, "#3ff4ff", "a"), (PX + 70, PY + 262, 1.1, "#ff9df0", "c"),
          (PX + PW - 66, PY + 270, 1.2, "#ffe34d", "b"), (PX + PW // 2 - 96, PY + 246, .7, "#3ff4ff", "d"), (PX + PW // 2 + 20, PY + 300, 1.3, "#9fffe9", "a"),
          (PX + 236, PY + 60, .7, "#ffffff", "c"), (PX + PW - 232, PY + 196, .7, "#ffffff", "b")]
around = [(PX - 70, PY + 70, 1.6, "#ffe34d", "b"), (PX - 150, PY + 210, 1.0, "#3ff4ff", "d"), (PX + PW + 80, PY + 96, 1.3, "#ff9df0", "a"),
          (PX + PW + 160, PY + 236, 1.7, "#ffffff", "c"), (150, 96, 1.2, "#ffffff", "a"), (W - 140, 70, 1.0, "#ffe34d", "d"),
          (wx - 34, wy + 6, 1.5, "#ffe34d", "c"), (wx + w1w + 34, wy + 70, 1.6, "#ffffff", "a"), (wx + 190, wy - 6, .9, "#ffffff", "d"),
          (wx + w1w - 150, wy + 90, 1.0, "#3ff4ff", "b")]
deco = "".join(spark(*a) for a in around)
deco += heart(PX - 168, PY + 108, 6, "#ff5fa8", "#ffd0e6", "bob a") + heart(PX + PW + 112, PY + 150, 5, "#b47bff", "#e6d4ff", "bob c")
deco += heart(96, 300, 4, "#ff5fa8", "#ffd0e6", "bob b") + heart(W - 150, 316, 4, "#3ff4ff", "#d9fdff", "bob d")
deco += star5(PX - 96, PY + 300, 1.9, "c") + star5(PX + PW + 190, PY + 60, 1.5, "a") + star5(220, 190, 1.2, "d") + star5(W - 232, 196, 1.3, "b")

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="padpilot: drive Claude Code and Codex with a PS5 controller">
<title>padpilot</title>
<defs>
  <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1a0340"/><stop offset=".6" stop-color="#12002c"/><stop offset="1" stop-color="#07001a"/></linearGradient>
  <linearGradient id="fade" gradientUnits="userSpaceOnUse" x1="0" y1="{HORIZON}" x2="0" y2="{H}"><stop offset="0" stop-color="#12002c"/><stop offset=".5" stop-color="#12002c" stop-opacity=".45"/><stop offset="1" stop-color="#12002c" stop-opacity="0"/></linearGradient>
  <linearGradient id="chrome" gradientUnits="userSpaceOnUse" x1="0" y1="{wy}" x2="0" y2="{wy + 7 * c1}">
    <stop offset="0" stop-color="#ffffff"/><stop offset=".36" stop-color="#c9d4ff"/><stop offset=".5" stop-color="#8f7bf5"/>
    <stop offset=".5" stop-color="#3a1f9c"/><stop offset=".72" stop-color="#a070f0"/><stop offset="1" stop-color="#ffd9fa"/>
  </linearGradient>
  <linearGradient id="rainbow" gradientUnits="userSpaceOnUse" x1="{wx}" y1="0" x2="{wx + w1w}" y2="0">
    <stop offset="0" stop-color="#ff3fd8"/><stop offset=".25" stop-color="#ffe34d"/><stop offset=".5" stop-color="#3ff4ff"/><stop offset=".75" stop-color="#a070ff"/><stop offset="1" stop-color="#ff3fd8"/>
  </linearGradient>
  <linearGradient id="shine" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".85"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
  <pattern id="glit" width="36" height="28" patternUnits="userSpaceOnUse"><rect x="4" y="5" width="3" height="3" fill="#fff"/><rect x="22" y="2" width="2" height="2" fill="#fff"/><rect x="14" y="17" width="3" height="3" fill="#fff"/><rect x="30" y="21" width="2" height="2" fill="#fff"/></pattern>
  <filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="34"/></filter>
  <image id="pad" x="{PX}" y="{PY}" width="{PW}" height="{PH}" href="data:image/png;base64,{PAD}"/>
  <mask id="padmask" mask-type="alpha" style="mask-type: alpha"><use href="#pad"/></mask>
  <g id="w1">{w1}</g><g id="tag">{t}</g><g id="press">{w2}</g>
  <clipPath id="w1clip">{w1}</clipPath>
  <clipPath id="frame"><rect width="{W}" height="{H}" rx="18"/></clipPath>
</defs>
<style>
  .tw0 {{ animation: tw 2.4s steps(2) infinite; }} .tw1 {{ animation: tw 3.1s steps(2) .8s infinite; }}
  @keyframes tw {{ 50% {{ opacity: .15; }} }}
  .spk {{ animation: spk 1.8s ease-in-out infinite; }}
  .a {{ animation-delay: 0s; }} .b {{ animation-delay: -.45s; }} .c {{ animation-delay: -.9s; }} .d {{ animation-delay: -1.35s; }}
  @keyframes spk {{ 0%, 100% {{ transform: scale(.15) rotate(0deg); opacity: 0; }} 50% {{ transform: scale(1) rotate(45deg); opacity: 1; }} }}
  .bob {{ animation: bob 2.6s ease-in-out infinite; }}
  @keyframes bob {{ 50% {{ transform: translateY(-7px); }} }}
  .sweep {{ animation: sweep 4.5s ease-in-out infinite; }} .sweep.w {{ animation-delay: .7s; }}
  @keyframes sweep {{ 0%, 55% {{ transform: translateX(-340px); }} 100% {{ transform: translateX({w1w + 120}px); }} }}
  .halo {{ animation: halo 6s linear infinite; }} .halo.h2 {{ animation-delay: -2s; }} .halo.h3 {{ animation-delay: -4s; }}
  @keyframes halo {{ 0%, 100% {{ opacity: 0; }} 33% {{ opacity: .6; }} 66% {{ opacity: 0; }} }}
  .glitter {{ animation: tw 1.1s steps(2) infinite; }}
  .cur {{ animation: cur 1s steps(1) infinite; }} @keyframes cur {{ 50% {{ opacity: 0; }} }}
  @media (prefers-reduced-motion: reduce) {{
    .tw0, .tw1, .spk, .bob, .sweep, .halo, .glitter, .cur {{ animation: none; }}
    .spk {{ opacity: 1; }} .sweep {{ opacity: 0; }} .halo {{ opacity: .3; }}
  }}
</style>
<g clip-path="url(#frame)">
  <rect width="{W}" height="{H}" fill="url(#sky)"/>
  {"".join(stars)}
  <g fill="none" stroke="#6f5cff" stroke-width="2" opacity=".7">{"".join(floor)}</g>
  <rect y="{HORIZON}" width="{W}" height="{H - HORIZON}" fill="url(#fade)"/>
  <rect y="{HORIZON - 1}" width="{W}" height="3" fill="#ff3fd8" opacity=".55"/>
</g>
<rect x="3" y="3" width="{W - 6}" height="{H - 6}" rx="16" fill="none" stroke="#ff3fd8" stroke-width="6"/>
<rect x="12" y="12" width="{W - 24}" height="{H - 24}" rx="10" fill="none" stroke="#ffe34d" stroke-width="2" stroke-dasharray="8 6"/>

<!-- the controller: colour-cycling glow, the artwork, a shine that sweeps across it, sparkles -->
<g filter="url(#blur)">
  <ellipse class="halo" cx="{MID}" cy="{PY + PH // 2}" rx="{PW // 2 - 10}" ry="{PH // 2 - 30}" fill="#ff3fd8" opacity="0"/>
  <ellipse class="halo h2" cx="{MID}" cy="{PY + PH // 2}" rx="{PW // 2 - 10}" ry="{PH // 2 - 30}" fill="#3ff4ff" opacity="0"/>
  <ellipse class="halo h3" cx="{MID}" cy="{PY + PH // 2}" rx="{PW // 2 - 10}" ry="{PH // 2 - 30}" fill="#8a5cff" opacity="0"/>
</g>
{deco}
<use href="#pad"/>
<g mask="url(#padmask)">
  <g class="sweep" transform="translate(-340 0)"><rect x="{PX}" y="{PY - 20}" width="110" height="{PH + 40}" fill="url(#shine)" transform="skewX(-22)" style="transform-origin: {PX}px {PY + PH}px"/></g>
</g>
{"".join(spark(*a) for a in on_pad)}

<!-- wordmark: lavender rim, dark rim, chrome fill with a rainbow tint, glitter, shine -->
<use href="#w1" fill="#07001a" stroke="#07001a" stroke-width="14" transform="translate(7 9)" opacity=".8"/>
<use href="#w1" fill="#b9a8ff" stroke="#b9a8ff" stroke-width="14"/>
<use href="#w1" fill="#0b0620" stroke="#0b0620" stroke-width="7"/>
<use href="#w1" fill="url(#chrome)"/>
<g clip-path="url(#w1clip)">
  <rect x="{wx}" y="{wy}" width="{w1w}" height="{7 * c1}" fill="url(#rainbow)" opacity=".3"/>
  <rect class="glitter" x="{wx}" y="{wy}" width="{w1w}" height="{7 * c1}" fill="url(#glit)" opacity=".8"/>
  <g class="sweep w" transform="translate(-340 0)"><rect x="{wx}" y="{wy - 10}" width="90" height="{7 * c1 + 20}" fill="url(#shine)" transform="skewX(-22)" style="transform-origin: {wx}px {wy + 7 * c1}px"/></g>
</g>

<!-- tagline and the blinking prompt -->
<use href="#tag" fill="#07001a" transform="translate(3 3)"/>
<use href="#tag" fill="#e9e2ff"/>
<use href="#press" fill="#07001a" transform="translate(4 4)"/>
<use href="#press" fill="#ff3fd8"/>
<path class="cur" d="M{px_ - 30} {py_} l{c2 * 3} {c2 * 3.5} l-{c2 * 3} {c2 * 3.5} z" fill="#ffe34d"/>
<rect class="cur" x="{px_ + width(press, c2) + c2 * 2}" y="{py_}" width="{c2 * 3}" height="{7 * c2}" fill="#3ff4ff"/>
</svg>
'''
open(OUT, "w").write(svg)
print("wrote", OUT, len(svg), "bytes;", W, "x", H)
