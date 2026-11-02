# Media tools (development only)

Regenerate the screenshots and short recordings of the demo page, and run the keyboard, reduced-motion, contrast and size checks.
Needs Node 18+ and Playwright (`npm i -D playwright` then `npx playwright install chromium`; or set `CHROME=1` to use the installed Chrome).

```
python -m twin.portfolio                         # build out/portfolio/
python -m http.server 8000 -d out/portfolio      # the graphs load JSON, so serve over HTTP
node tools/media/shoot.js http://localhost:8000 out/portfolio/media tools/media/plan_rec.json   # reasoning-graph shots and recordings
node tools/media/shoot.js http://localhost:8000 out/portfolio/media tools/media/plan_ux.json    # overview, drawer, To resolve, analyst: light and dark, 1440 and 390 px
node tools/media/shoot.js http://localhost:8000 out/portfolio/media tools/media/plan_t16.json   # first screen, drawer, three panels, For reviewers, stake slider: light and dark, 1440 and 390 px
node tools/media/a11y.js  http://localhost:8000            # keyboard and reduced-motion checks
node tools/media/a11y.js  http://localhost:8000 audit      # contrast (4.5:1), text size (12 px), page width, engine words on the first screen
```
`plan_*.json` list each shot: page anchor, actions, light or dark, size, reduced motion, and whether to record video.
`audit_page.js` is the in-page audit; it also runs in any browser console (`window.AUDIT_ROOT` limits it to one element).
These scripts are not part of the Python checks.
