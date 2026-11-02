# Media tools (development only)

Regenerate the screenshots and short recordings of the demo page, and run the keyboard / reduced-motion checks.
Needs Node 18+ and Playwright (`npm i -D playwright` then `npx playwright install chromium`).

```
python -m twin.portfolio                         # build out/portfolio/
python -m http.server 8000 -d out/portfolio      # the graphs load JSON, so serve over HTTP
node tools/media/shoot.js http://localhost:8000 out/portfolio/media tools/media/plan_rec.json
node tools/media/a11y.js  http://localhost:8000
```
`plan_rec.json` lists each shot: page anchor, actions, light or dark, reduced motion, and whether to record video.
These scripts are not part of the Python checks.
