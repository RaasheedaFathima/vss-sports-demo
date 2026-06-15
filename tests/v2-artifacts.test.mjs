import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const html = fs.readFileSync("frontend/index.html", "utf8");
const backend = fs.readFileSync("app/main.py", "utf8");

test("frontend keeps Gemini shell while switching upload to Object Storage", () => {
  assert.match(html, /VSS2 Gemini Style/);
  assert.match(html, /Oracle Video Race Analysis/);
  assert.match(html, /marathon-vlm-inputs/);
  assert.match(html, /\/api\/upload-to-storage/);
  assert.match(html, /\/api\/analyze-from-storage/);
  assert.match(html, /Upload to Object Storage/);
});

test("desktop hero composer uses wide columns so labels are not clipped", () => {
  assert.match(html, /home-inner \{ width: min\(1040px, calc\(100vw - 160px\)\)/);
  assert.match(html, /composer \{[\s\S]*width: min\(960px, 100%\)/);
  assert.match(html, /grid-template-columns: auto minmax\(260px, 1fr\) minmax\(190px, 220px\) minmax\(140px, 170px\) minmax\(240px, 270px\)/);
  assert.match(html, /white-space: nowrap/);
});

test("frontend uses Gemini-like Google Sans typography", () => {
  assert.match(html, /fonts\.googleapis\.com\/css2\?family=Roboto\+Flex:opsz,wght@8\.\.144,100\.\.1000/);
  assert.match(html, /--sans: "Roboto Flex", "Google Sans", "Product Sans", Roboto, "Helvetica Neue", Arial, sans-serif;/);
  assert.match(html, /-webkit-font-smoothing: antialiased;/);
  assert.match(html, /--text: #e8eaed;/);
  assert.match(html, /font-synthesis: none;/);
  assert.match(html, /hero-brand h1 \{[\s\S]*font-size: clamp\(38px, 3\.8vw, 58px\);[\s\S]*font-weight: 220; letter-spacing: 0; color: #dfe1e5;/);
  assert.match(html, /font-variation-settings: "opsz" 72, "wght" 220;/);
  assert.match(html, /file-drop \{[\s\S]*font-size: 18px; font-weight: 300;/);
  assert.match(html, /file-drop strong \{[\s\S]*font-weight: 400;/);
  assert.match(html, /select-pill, \.input-pill \{[\s\S]*font-size: 17px; font-weight: 350;/);
  assert.match(html, /analyze-btn \{[\s\S]*font-weight: 500;/);
});

test("backend exposes v2-only Object Storage upload and analyze routes", () => {
  assert.match(backend, /@app\.post\("\/api\/upload-to-storage"\)/);
  assert.match(backend, /@app\.post\("\/api\/analyze-from-storage"\)/);
  assert.match(backend, /@app\.get\("\/api\/audit"\)/);
  assert.match(backend, /marathon-vlm-inputs/);
  assert.match(backend, /oci:\/\/\{OBJECT_STORAGE_NAMESPACE\}\/\{OBJECT_STORAGE_BUCKET\}/);
});

test("timeline tab renders second-by-second race output", () => {
  assert.match(backend, /\\"second_by_second\\": \[\{\\"second\\": number/);
  assert.match(backend, /Populate second_by_second for every second/);
  assert.match(html, /summary\.second_by_second/);
  assert.match(html, /function formatSecondBySecond/);
  assert.match(html, /\$\{Math\.round\(Number\(second\)\)\}s`/);
});

test("raw narrative analyses still populate timeline and runners tabs", () => {
  assert.match(html, /else if \(state\.activeTab === 'timeline'\) \$\('analysis-content'\)\.textContent = formatTimeline\(summary\);/);
  assert.match(html, /else if \(state\.activeTab === 'runners'\) \$\('analysis-content'\)\.textContent = formatRunners\(summary\);/);
  assert.match(html, /if \(summary\.raw\) return formatRawTimeline\(summary\.raw\);/);
  assert.match(html, /function extractRawTimeline/);
  assert.match(html, /function formatRawRunners/);
  assert.match(html, /Time:\\s\*\(\[0-9:\.\]\+\)\\s\*\[-–\]/);
  assert.doesNotMatch(backend, /"raw": str\(raw\)\[:2000\]/);
  assert.match(backend, /"raw": str\(raw\)/);
});

test("race app defaults uploads to sports scenario", () => {
  assert.match(html, /scenario\.id === 'sports'/);
  assert.match(html, /\$\('upload-scenario'\)\.value = 'sports'/);
});

test("frontend exposes audit control board from left navigation", () => {
  assert.match(html, /data-panel="audit" title="Audit control board"/);
  assert.match(html, /Audit control board/);
  assert.match(html, /\/api\/audit\?limit=50/);
  assert.match(html, /estimated_total_text_tokens/);
  assert.match(html, /audit-grid/);
});

test("backend normalizes Oracle LOB values before status JSON responses", () => {
  const getVideo = backend.match(/def _db_get_video[\s\S]*?def _db_list_videos/)?.[0] || "";
  assert.match(getVideo, /_json_safe\(dict\(zip\(cols, row\)\)\)/);
  assert.match(backend, /return JSONResponse\(_json_safe\(resp\)\)/);
});
