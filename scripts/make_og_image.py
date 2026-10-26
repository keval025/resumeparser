"""Render static/og-image.png (1200x630, the social-media preview) with Playwright.

Usage:  python scripts/make_og_image.py ["App name"]
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
NAME = sys.argv[1] if len(sys.argv) > 1 else "Resume Parser"

HTML = f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin: 0; width: 1200px; height: 630px; font-family: "Segoe UI", system-ui, sans-serif;
  background: linear-gradient(135deg, #312e81 0%, #4f46e5 55%, #6366f1 100%); color: #fff; display: flex; align-items: center; }}
.wrap {{ display: flex; gap: 56px; align-items: center; padding: 0 80px; width: 100%; box-sizing: border-box; }}
.text {{ flex: 1.2; }}
.brand {{ display: flex; align-items: center; gap: 14px; font-size: 30px; font-weight: 700; opacity: .95; }}
.brand svg {{ width: 52px; height: 52px; }}
h1 {{ font-size: 62px; line-height: 1.06; margin: 28px 0 18px; letter-spacing: -1px; }}
p {{ font-size: 27px; margin: 0; opacity: .88; }}
.card {{ flex: .8; background: #fff; color: #1d2130; border-radius: 28px; padding: 34px; box-shadow: 0 30px 60px rgba(0,0,0,.25); }}
.ring {{ width: 170px; height: 170px; border-radius: 50%; margin: 0 auto 22px; display: grid; place-items: center;
  background: conic-gradient(#157f3b 82%, #e9ebf2 0); }}
.ring span {{ width: 132px; height: 132px; border-radius: 50%; background: #fff; display: grid; place-items: center; font-size: 58px; font-weight: 800; }}
.tag {{ text-align: center; font-size: 24px; font-weight: 700; color: #157f3b; margin-bottom: 18px; }}
.row {{ display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; }}
.chip {{ font-size: 19px; border-radius: 999px; padding: 6px 16px; background: #fde8e7; color: #b42318; }}
.chip.ok {{ background: #e3f5e9; color: #157f3b; }}
</style></head><body><div class="wrap">
<div class="text">
  <div class="brand"><svg viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#fff"/><path d="M10 7h8l5 5v12a2 2 0 0 1-2 2H10a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z" fill="#4f46e5"/><path d="M11.5 19.5l3 3 6-7" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>{NAME}</div>
  <h1>Your resume match score for any job in 30 seconds</h1>
  <p>See the gaps, tailor your resume, know where to apply first.</p>
</div>
<div class="card"><div class="ring"><span>82</span></div><div class="tag">Strong · Apply now</div>
<div class="row"><span class="chip ok">SQL ✓</span><span class="chip ok">Python ✓</span><span class="chip">Tableau</span></div></div>
</div></body></html>"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1200, "height": 630})
    page.set_content(HTML)
    page.screenshot(path=str(ROOT / "static" / "og-image.png"))
    browser.close()
print("wrote static/og-image.png")
