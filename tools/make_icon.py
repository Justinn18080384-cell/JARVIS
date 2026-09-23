"""Erzeugt das JARVIS-App-Icon (Arc-Reactor-Motiv) als .ico und .png."""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "jarvis" / "assets"
OUT.mkdir(parents=True, exist_ok=True)
S = 1024
C = S / 2
CYAN = (25, 211, 255)


def ring(d, r, w, color, segs=None):
    box = [C - r, C - r, C + r, C + r]
    if segs is None:
        d.ellipse(box, outline=color, width=w)
    else:
        for a, b in segs:
            d.arc(box, a, b, fill=color, width=w)


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# dunkler runder Hintergrund
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(bg).ellipse([24, 24, S - 24, S - 24], fill=(4, 12, 22, 255), outline=(25, 211, 255, 120), width=10)
img.alpha_composite(bg)

glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
g = ImageDraw.Draw(glow)
ring(g, 420, 26, CYAN + (255,), [(i * 45 + 6, i * 45 + 39) for i in range(8)])
ring(g, 350, 10, CYAN + (200,))
ring(g, 290, 40, CYAN + (255,), [(i * 30 + 4, i * 30 + 22) for i in range(12)])
# Dreieck (Arc Reactor)
pts = [(C + math.cos(a) * 225, C + math.sin(a) * 225) for a in [(i / 3) * 2 * math.pi - math.pi / 2 for i in range(3)]]
g.polygon(pts, outline=CYAN + (230,), width=18)
g.ellipse([C - 110, C - 110, C + 110, C + 110], fill=CYAN + (255,))
blur = glow.filter(ImageFilter.GaussianBlur(28))
img.alpha_composite(blur)
img.alpha_composite(glow)
core = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(core).ellipse([C - 70, C - 70, C + 70, C + 70], fill=(235, 252, 255, 255))
img.alpha_composite(core.filter(ImageFilter.GaussianBlur(10)))

img.save(OUT / "jarvis.png")
img.save(OUT / "jarvis.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("Icon erstellt:", OUT)
