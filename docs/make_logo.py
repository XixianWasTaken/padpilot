"""Usage: python3 docs/make_logo.py docs/logo.svg

Generate docs/logo.svg: a 2000s pixel banner. Text is drawn as pixel rects (no web fonts:
GitHub shows SVGs as images, which cannot load fonts)."""
import sys
OUT = sys.argv[1]
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
def text_rects(s, x0, y0, cell, gap=1):
    out, x = [], x0
    for ch in s:
        g = FONT[ch]
        for r, row in enumerate(g):
            c = 0
            while c < 5:                      # merge runs into one rect per row
                if row[c] == "#":
                    k = c
                    while k < 5 and row[k] == "#":
                        k += 1
                    out.append(f'<rect x="{x + c * cell}" y="{y0 + r * cell}" width="{(k - c) * cell}" height="{cell}"/>')
                    c = k
                else:
                    c += 1
        x += (5 + gap) * cell
    return "".join(out), x - gap * cell
def width(s, cell, gap=1):
    return len(s) * (5 + gap) * cell - gap * cell

W, H = 1280, 400
STAR = "M0 -9 L2.6 -2.6 L9 0 L2.6 2.6 L0 9 L-2.6 2.6 L-9 0 L-2.6 -2.6 Z"
SHELL = "M128 74 Q150 60 214 60 L426 60 Q490 60 512 74 Q546 96 560 150 L596 300 Q610 372 556 388 Q512 400 480 340 L450 292 L190 292 L160 340 Q128 400 84 388 Q30 372 44 300 L80 150 Q94 96 128 74 Z"
CORE = "M100 128 Q112 100 160 100 L480 100 Q528 100 540 128 L566 268 Q570 292 540 292 L100 292 Q70 292 74 268 Z"
L2 = "M114 62 Q114 -4 168 -12 L228 -12 Q250 -10 250 10 L250 50 Z"
L1 = "M84 124 Q92 40 196 28 L254 28 L254 74 Z"
MIR = 'transform="matrix(-1 0 0 1 640 0)"'

word1, word2 = "DUALSENSE", "AGENT"
c1, c2, c3 = 13, 13, 4
wx = 500
w1, _ = text_rects(word1, wx, 92, c1)
w2, _ = text_rects(word2, wx, 92 + 7 * c1 + 26, c2)
tag = "DRIVE CLAUDE CODE + CODEX WITH A PS5 CONTROLLER"
tx = (W - width(tag, c3)) // 2
t, _ = text_rects(tag, tx, 342, c3)
w1w = width(word1, c1)

stars = []
import random
random.seed(7)
for _ in range(70):
    x, y = random.randrange(0, W), random.randrange(0, H)
    col = random.choice(["#ffffff", "#ffffff", "#3ff4ff", "#ff3fd8", "#ffe34d"])
    s = random.choice([2, 2, 3])
    stars.append(f'<rect x="{x}" y="{y}" width="{s}" height="{s}" fill="{col}" class="tw{random.randint(0, 2)}"/>')

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="dualsense-agent: drive Claude Code and Codex with a PS5 controller">
<title>dualsense-agent</title>
<defs>
  <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2a0058"/><stop offset=".55" stop-color="#14002e"/><stop offset="1" stop-color="#0a0019"/></linearGradient>
  <linearGradient id="rainbow" gradientUnits="userSpaceOnUse" x1="{wx}" y1="0" x2="{wx + w1w}" y2="0">
    <stop offset="0" stop-color="#ff3fd8"/><stop offset=".35" stop-color="#ffe34d"/><stop offset=".7" stop-color="#3ff4ff"/><stop offset="1" stop-color="#ff3fd8"/>
  </linearGradient>
  <linearGradient id="g-shell" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset=".55" stop-color="#f1ecfb"/><stop offset="1" stop-color="#d9d1ee"/></linearGradient>
  <linearGradient id="g-core" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#a98bf0"/><stop offset=".6" stop-color="#7d5fd6"/><stop offset="1" stop-color="#5b3fb4"/></linearGradient>
  <linearGradient id="g-sh" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#d6cdef"/></linearGradient>
  <radialGradient id="g-key" cx=".38" cy=".3" r=".85"><stop offset="0" stop-color="#ffffff"/><stop offset=".65" stop-color="#efeaff"/><stop offset="1" stop-color="#c9bdee"/></radialGradient>
  <radialGradient id="g-stick" cx=".4" cy=".35" r=".75"><stop offset="0" stop-color="#4a3a86"/><stop offset="1" stop-color="#241850"/></radialGradient>
  <pattern id="glit" width="22" height="22" patternUnits="userSpaceOnUse">
    <rect x="3" y="4" width="2" height="2" fill="#ff3fd8"/><rect x="14" y="2" width="2" height="2" fill="#3ff4ff"/>
    <rect x="9" y="12" width="2" height="2" fill="#ffe34d"/><rect x="18" y="16" width="2" height="2" fill="#ff3fd8"/><rect x="4" y="18" width="2" height="2" fill="#a8ff3f"/>
  </pattern>
  <pattern id="sparkle-text" width="9" height="9" patternUnits="userSpaceOnUse"><rect x="2" y="2" width="2" height="2" fill="#fff" opacity=".85"/></pattern>
  <filter id="glow" x="-300%" y="-60%" width="700%" height="220%"><feGaussianBlur stdDeviation="7"/></filter>
  <clipPath id="w1clip">{w1}</clipPath>
</defs>
<style>
  .tw0 {{ animation: tw 2.4s steps(2) infinite; }} .tw1 {{ animation: tw 3.1s steps(2) .8s infinite; }} .tw2 {{ animation: none; }}
  @keyframes tw {{ 50% {{ opacity: .15; }} }}
  .spk {{ animation: spk 1.4s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
  .spk.b {{ animation-delay: .5s; }} .spk.c {{ animation-delay: .9s; }}
  @keyframes spk {{ 0%, 100% {{ transform: scale(.35); opacity: .45; }} 50% {{ transform: scale(1) rotate(45deg); opacity: 1; }} }}
  .lb {{ animation: lb 2.2s ease-in-out infinite; }}
  @keyframes lb {{ 50% {{ fill: #3ff4ff; }} }}
  .cur {{ animation: cur 1s steps(1) infinite; }} @keyframes cur {{ 50% {{ opacity: 0; }} }}
  @media (prefers-reduced-motion: reduce) {{ .tw0, .tw1, .spk, .lb, .cur {{ animation: none; }} }}
</style>
<rect width="{W}" height="{H}" rx="18" fill="url(#sky)"/>
{"".join(stars)}
<rect x="0" y="0" width="{W}" height="{H}" rx="18" fill="none" stroke="#ff3fd8" stroke-width="6"/>
<rect x="9" y="9" width="{W - 18}" height="{H - 18}" rx="12" fill="none" stroke="#ffe34d" stroke-width="2" stroke-dasharray="8 6"/>

<!-- the controller -->
<g transform="translate(64 62) scale(.6)">
  <path d="{L2}" fill="url(#g-sh)" stroke="#0b0620" stroke-width="4"/><path d="{L2}" {MIR} fill="url(#g-sh)" stroke="#0b0620" stroke-width="4"/>
  <path d="{L1}" fill="url(#g-sh)" stroke="#0b0620" stroke-width="4"/><path d="{L1}" {MIR} fill="url(#g-sh)" stroke="#0b0620" stroke-width="4"/>
  <path d="{SHELL}" transform="translate(10 11)" fill="#07001a"/>
  <path d="{SHELL}" fill="url(#g-shell)" stroke="#0b0620" stroke-width="5" stroke-linejoin="round"/>
  <path d="{SHELL}" fill="url(#glit)" opacity=".55"/>
  <path d="{CORE}" fill="url(#g-core)" stroke="#0b0620" stroke-width="4"/>
  <path d="{CORE}" fill="url(#glit)" opacity=".7"/>
  <rect class="lb" x="217" y="72" width="14" height="96" rx="7" fill="#ff3fd8" filter="url(#glow)"/>
  <rect class="lb" x="409" y="72" width="14" height="96" rx="7" fill="#ff3fd8" filter="url(#glow)"/>
  <rect x="236" y="70" width="168" height="98" rx="14" fill="#fbf9ff" stroke="#0b0620" stroke-width="3"/>
  <rect class="lb" x="221" y="78" width="8" height="84" rx="4" fill="#ff3fd8"/><rect class="lb" x="411" y="78" width="8" height="84" rx="4" fill="#ff3fd8"/>
  <g fill="url(#g-key)" stroke="#0b0620" stroke-width="3">
    <rect x="153" y="116" width="26" height="32" rx="5"/><rect x="153" y="176" width="26" height="32" rx="5"/>
    <rect x="119" y="149" width="32" height="26" rx="5"/><rect x="181" y="149" width="32" height="26" rx="5"/>
    <circle cx="474" cy="124" r="19"/><circle cx="512" cy="162" r="19"/><circle cx="474" cy="200" r="19"/><circle cx="436" cy="162" r="19"/>
  </g>
  <path d="M474 113 l10 16 h-20z" fill="none" stroke="#0fae7c" stroke-width="3.5" stroke-linejoin="round"/>
  <circle cx="512" cy="162" r="8.5" fill="none" stroke="#f0325a" stroke-width="3.5"/>
  <path d="M467 193 l14 14 M481 193 l-14 14" stroke="#2f62ff" stroke-width="3.5" stroke-linecap="round"/>
  <rect x="429" y="155" width="14" height="14" rx="1.5" fill="none" stroke="#d42fc9" stroke-width="3.5"/>
  <g fill="url(#g-stick)" stroke="#0b0620" stroke-width="3"><circle cx="238" cy="240" r="38"/><circle cx="402" cy="240" r="38"/><circle cx="320" cy="226" r="14"/></g>
  <g fill="none" stroke="#8f7fd0" stroke-width="3" stroke-dasharray="2 4"><circle cx="238" cy="240" r="27"/><circle cx="402" cy="240" r="27"/></g>
  <circle cx="474" cy="200" r="27" fill="none" stroke="#ffe34d" stroke-width="4" stroke-dasharray="6 4"/>
  <path class="spk" transform="translate(96 338) scale(1.6)" d="{STAR}" fill="#ffe34d" stroke="#b48a00"/>
  <path class="spk b" transform="translate(548 338) scale(1.2)" d="{STAR}" fill="#3ff4ff"/>
</g>

<!-- wordmark -->
<g fill="#07001a" transform="translate(6 6)">{w1}</g>
<g fill="url(#rainbow)">{w1}</g>
<rect x="{wx}" y="92" width="{w1w}" height="{7 * c1}" fill="url(#sparkle-text)" clip-path="url(#w1clip)"/>
<g fill="#07001a" transform="translate(6 6)">{w2}</g>
<g fill="#ff3fd8">{w2}</g>
<rect class="cur" x="{wx + width(word2, c2) + c2 * 2}" y="{92 + 7 * c1 + 26}" width="{c2 * 3}" height="{7 * c2}" fill="#3ff4ff"/>
<path class="spk" transform="translate({wx - 22} 84) scale(1.4)" d="{STAR}" fill="#ffe34d"/>
<path class="spk b" transform="translate({wx + w1w + 20} 98) scale(1.1)" d="{STAR}" fill="#ffffff"/>
<path class="spk c" transform="translate({wx + w1w - 30} {92 + 7 * c1 + 72}) scale(1.6)" d="{STAR}" fill="#3ff4ff"/>

<!-- tagline -->
<rect x="{tx - 18}" y="326" width="{width(tag, c3) + 36}" height="{7 * c3 + 28}" fill="#ff3fd8"/>
<rect x="{tx - 18}" y="326" width="{width(tag, c3) + 36}" height="{7 * c3 + 28}" fill="none" stroke="#ffe34d" stroke-width="3"/>
<g fill="#14002e">{t}</g>
</svg>
'''
open(OUT, "w").write(svg)
print("wrote", OUT, len(svg), "bytes")
