#!/usr/bin/env node
// Render every raster brand asset from the vector source in docs/brand/.
//
// The mark is drawn in code everywhere it can be: inline SVG on the homepage
// and in the console, SVG favicons. Platforms that insist on bitmaps (the iOS
// home screen icon, the PNG app icon, favicon.ico, the social preview) get
// PNGs rendered here from the same SVG, so the mark has one definition.
//
//   node scripts/render_brand.mjs        (needs Playwright with Chromium)

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const brand = (name) => readFileSync(join(ROOT, "docs/brand", name), "utf8");
const sized = (svg, size) => svg.replace(/width="\d+" height="\d+"/, `width="${size}" height="${size}"`);

const og = `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@500;700&display=swap">
<style>
  body { margin: 0; width: 1200px; height: 630px; background: #FAFBFC; color: #0B0F14; font-family: Poppins, sans-serif; position: relative; overflow: hidden; }
  .word { position: absolute; left: 80px; top: 72px; display: flex; align-items: flex-end; font-weight: 700; font-size: 56px; line-height: .8; letter-spacing: -2px; }
  .word svg { width: 62px; height: 52px; margin-right: -2px; }
  h1 { position: absolute; left: 80px; top: 214px; margin: 0; font-size: 80px; line-height: 1.04; letter-spacing: -3px; font-weight: 700; }
  h1 span { display: block; color: #667085; }
  .rule { position: absolute; left: 80px; right: 80px; bottom: 96px; height: 8px; border-radius: 4px; background: #E5E7EB; }
  .rule i { position: absolute; left: 0; top: 0; bottom: 0; width: 34%; border-radius: 4px; background: #00C2FF; }
  .url { position: absolute; right: 80px; top: 92px; font-weight: 500; font-size: 24px; color: #667085; }
</style></head><body>
  <div class="word">${brand("mark.svg")}<span>eynod</span></div>
  <div class="url">veynod.com</div>
  <h1>Agents do the work.<span>Humans hold the line.</span></h1>
  <div class="rule"><i></i></div>
</body></html>`;

const browser = await chromium.launch();
const page = await browser.newPage();
async function icon(svg, size, path) {
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(`<html><body style="margin:0;background:transparent">${sized(svg, size)}</body></html>`);
  await page.screenshot({ path: join(ROOT, path), omitBackground: true });
}
const dark = brand("icon-dark.svg");
await icon(dark, 180, "app/static/apple-touch-icon.png");
await icon(dark, 180, "dashboard/app/apple-icon.png");
await icon(dark, 512, "dashboard/app/icon.png");

// favicon.ico: 16, 32 and 48 px PNG frames in one ICO container.
const frames = [];
for (const size of [16, 32, 48]) {
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(`<html><body style="margin:0;background:transparent">${sized(dark, size)}</body></html>`);
  frames.push([size, await page.screenshot({ omitBackground: true })]);
}
const header = Buffer.alloc(6 + 16 * frames.length);
header.writeUInt16LE(0, 0);
header.writeUInt16LE(1, 2);
header.writeUInt16LE(frames.length, 4);
let offset = header.length;
frames.forEach(([size, png], index) => {
  const entry = 6 + 16 * index;
  header.writeUInt8(size, entry);
  header.writeUInt8(size, entry + 1);
  header.writeUInt16LE(1, entry + 4);
  header.writeUInt16LE(32, entry + 6);
  header.writeUInt32LE(png.length, entry + 8);
  header.writeUInt32LE(offset, entry + 12);
  offset += png.length;
});
writeFileSync(join(ROOT, "dashboard/app/favicon.ico"), Buffer.concat([header, ...frames.map(([, png]) => png)]));

await page.setViewportSize({ width: 1200, height: 630 });
await page.setContent(og, { waitUntil: "networkidle" });
await page.screenshot({ path: join(ROOT, "app/static/og.png") });
await browser.close();
console.log("brand assets rendered");
