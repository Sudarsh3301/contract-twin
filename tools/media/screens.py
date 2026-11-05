"""README pictures of the demo's first screen, light and dark, from the built site.
Run from repo root:  python -m http.server 8765 --directory site   (in another shell), then
                     python tools/media/screens.py http://localhost:8765/index.html docs/img"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

url, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    for scheme in ("light", "dark"):
        pg = b.new_page(viewport={"width": 1440, "height": 1200}, color_scheme=scheme, device_scale_factor=1)
        pg.goto(url, wait_until="networkidle")
        pg.wait_for_timeout(800)
        top = pg.get_by_role("button", name="IPO $200M").first.bounding_box()     # from the deal buttons down: the answer
        pg.screenshot(path=str(out / f"demo-{scheme}.png"), clip={"x": 0, "y": top["y"] - 12, "width": 1440, "height": 760})
        pg.close()
        print("wrote", out / f"demo-{scheme}.png")
    b.close()
