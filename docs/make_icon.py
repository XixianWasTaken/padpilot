"""Usage: make_icon.py docs/controller.png .claude-plugin/icon.png   (needs Pillow)

The plugin directory icon: the banner's pixel controller on the banner's night sky, 1024 x 1024."""
import sys
from PIL import Image, ImageDraw, ImageFilter

src, out = sys.argv[1:3]
N = 1024
SKY = [(0.0, (0x1a, 0x03, 0x40)), (0.6, (0x12, 0x00, 0x2c)), (1.0, (0x07, 0x00, 0x1a))]


def sky(t):
    for (a, ca), (b, cb) in zip(SKY, SKY[1:]):
        if t <= b:
            f = (t - a) / (b - a)
            return tuple(round(x + (y - x) * f) for x, y in zip(ca, cb))
    return SKY[-1][1]


img = Image.new("RGBA", (N, N))
d = ImageDraw.Draw(img)
for y in range(N):
    d.line([(0, y), (N, y)], fill=sky(y / (N - 1)) + (255,))

# perspective floor grid in the lower third, like the banner
HOR = 742
grid = Image.new("RGBA", (N, N), (0, 0, 0, 0))
g = ImageDraw.Draw(grid)
for i in range(-12, 13):
    g.line([(N / 2 + i * 26, HOR), (N / 2 + i * 210, N)], fill=(0x6f, 0x5c, 0xff, 150), width=3)
y, step = HOR + 8, 10
while y < N:
    g.line([(0, y), (N, y)], fill=(0x6f, 0x5c, 0xff, 150), width=3)
    step *= 1.45
    y += step
g.rectangle([0, HOR - 2, N, HOR + 2], fill=(0xff, 0x3f, 0xd8, 170))
img.alpha_composite(grid)

# soft glow behind the controller
pad = Image.open(src).convert("RGBA")
W = 900
H = round(pad.height * W / pad.width)
pad = pad.resize((W, H), Image.LANCZOS)
px, py = (N - W) // 2, 236
glow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse([px + 40, py + 20, px + W - 40, py + H - 10], fill=(0xff, 0x3f, 0xd8, 120))
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(70)))
glow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse([px + 160, py + 60, px + W - 160, py + H - 60], fill=(0x3f, 0xf4, 0xff, 70))
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60)))

# pixel sparkles (4-point stars drawn on an 8 px grid)
def sparkle(cx, cy, s, col):
    u = 8 * s
    for dx, dy, w, h in ((-1, -3, 2, 6), (-3, -1, 6, 2)):
        d2.rectangle([cx + dx * u / 2 - 2, cy + dy * u / 2 - 2, cx + (dx + w) * u / 2 + 1, cy + (dy + h) * u / 2 + 1], fill=(0x0b, 0x06, 0x20, 255))
    for dx, dy, w, h in ((-1, -3, 2, 6), (-3, -1, 6, 2)):
        d2.rectangle([cx + dx * u / 2, cy + dy * u / 2, cx + (dx + w) * u / 2 - 1, cy + (dy + h) * u / 2 - 1], fill=col)


d2 = ImageDraw.Draw(img)
for x, y, s in ((84, 96, 2), (600, 70, 1), (240, 170, 1), (930, 140, 2), (950, 640, 1), (70, 610, 1)):
    sparkle(x, y, s, (255, 255, 255, 255) if s == 1 else (0xff, 0xe3, 0x4d, 255))
for x, y, c in ((420, 120, (0x3f, 0xf4, 0xff)), (790, 210, (0xff, 0x3f, 0xd8)), (150, 300, (0xb9, 0xa8, 0xff)), (880, 560, (0x3f, 0xf4, 0xff))):
    d2.rectangle([x, y, x + 7, y + 7], fill=c + (255,))

img.alpha_composite(pad, (px, py))
img.convert("RGB").save(out, optimize=True)
print("wrote", out, img.size)
