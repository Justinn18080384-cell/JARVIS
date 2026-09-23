"""Erzeugt das JARVIS-App-Icon (Dark & Gold: goldene Raute mit Ringen und Kugel) als .ico und .png."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "jarvis" / "assets"
OUT.mkdir(parents=True, exist_ok=True)
S = 1024
C = S / 2
GOLD = (212, 175, 55)
GOLD_LIGHT = (246, 221, 150)


def diamond(r):
    return [(C, C - r), (C + r, C), (C, C + r), (C - r, C)]


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# tiefschwarzer runder Hintergrund mit feiner Goldkante
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(bg).ellipse([24, 24, S - 24, S - 24], fill=(10, 8, 5, 255), outline=GOLD + (150,), width=8)
img.alpha_composite(bg)

art = Image.new("RGBA", (S, S), (0, 0, 0, 0))
g = ImageDraw.Draw(art)
# zwei feine Ringe mit Lücken (wie der Kern in der App)
for r, w, a0 in ((400, 10, 20), (350, 16, 200)):
    box = [C - r, C - r, C + r, C + r]
    g.arc(box, a0, a0 + 140, fill=GOLD + (230,), width=w)
    g.arc(box, a0 + 180, a0 + 320, fill=GOLD + (230,), width=w)
# große und kleine Raute
g.polygon(diamond(300), outline=GOLD_LIGHT + (255,), width=22)
g.polygon(diamond(170), outline=GOLD + (220,), width=14)
# goldene Kugel in der Mitte
g.ellipse([C - 95, C - 95, C + 95, C + 95], fill=GOLD + (255,))

img.alpha_composite(art.filter(ImageFilter.GaussianBlur(22)))   # weiches Leuchten
img.alpha_composite(art)
shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shine).ellipse([C - 60, C - 70, C + 30, C + 20], fill=(255, 248, 225, 255))
img.alpha_composite(shine.filter(ImageFilter.GaussianBlur(18)))  # Glanzpunkt auf der Kugel

img.save(OUT / "jarvis.png")
img.save(OUT / "jarvis.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("Icon erstellt:", OUT)
