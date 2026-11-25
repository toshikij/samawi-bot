"""Tile PNGs into one contact sheet: python scripts/contact_sheet.py OUT.png IN1.png IN2.png …"""
import sys
from PIL import Image

out, paths = sys.argv[1], sys.argv[2:]
imgs = [Image.open(p).convert("RGB") for p in paths]
w = 360
thumbs = [im.resize((w, int(im.height * w / im.width))) for im in imgs]
cols = min(5, len(thumbs))
rows = -(-len(thumbs) // cols)
h = max(t.height for t in thumbs)
sheet = Image.new("RGB", (cols * (w + 12) + 12, rows * (h + 12) + 12), (40, 40, 40))
for i, t in enumerate(thumbs):
    sheet.paste(t, (12 + (i % cols) * (w + 12), 12 + (i // cols) * (h + 12)))
sheet.save(out)
