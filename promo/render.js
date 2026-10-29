// Renders promo.html frame by frame and encodes it (with soundtrack.wav) to MP4.
//
//   python3 promo/soundtrack.py          # -> promo/soundtrack.wav
//   node promo/render.js                 # -> promo/veynod-promo.mp4
//   node promo/render.js --stills 3,9,17 # -> PNG stills for quick checks
//
// Requires Playwright (Chromium) and ffmpeg with libx264.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const FPS = 30;
const dir = __dirname;
const args = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  await page.goto('file://' + path.join(dir, 'promo.html'), { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForLoadState('networkidle');
  const duration = await page.evaluate(() => window.DURATION);

  const stillsIdx = args.indexOf('--stills');
  if (stillsIdx >= 0) {
    const out = path.join(dir, 'stills');
    fs.mkdirSync(out, { recursive: true });
    for (const t of args[stillsIdx + 1].split(',').map(Number)) {
      await page.evaluate(t => window.render(t), t);
      await page.screenshot({ path: path.join(out, `t${t}.png`) });
    }
    await browser.close();
    return;
  }

  const audio = path.join(dir, 'soundtrack.wav');
  const ff = spawn('ffmpeg', [
    '-y', '-loglevel', 'error',
    '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'mjpeg', '-i', '-',
    ...(fs.existsSync(audio) ? ['-i', audio, '-c:a', 'aac', '-b:a', '192k', '-shortest'] : []),
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
    path.join(dir, 'veynod-promo.mp4'),
  ], { stdio: ['pipe', 'inherit', 'inherit'] });

  const frames = Math.round(duration * FPS);
  for (let i = 0; i < frames; i++) {
    await page.evaluate(t => window.render(t), i / FPS);
    const buf = await page.screenshot({ type: 'jpeg', quality: 92 });
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    if (i % 150 === 0) process.stdout.write(`frame ${i}/${frames}\n`);
  }
  ff.stdin.end();
  await new Promise(r => ff.on('close', r));
  await browser.close();
  console.log('done: promo/veynod-promo.mp4');
})();
