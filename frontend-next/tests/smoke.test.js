"use strict";
// Frontend smoke tests — no DOM, no framework, just structural checks that
// catch the most common deploy-time regressions (missing env, broken
// middleware, etc). Run with `npm test`.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");

test("package.json declares the production build script", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  assert.equal(pkg.scripts.build, "next build", "build script must be `next build`");
  assert.ok(pkg.scripts.start, "start script must exist for `next start` deploys");
});

test("next.config.ts uses standalone output for smaller Docker images", () => {
  const src = fs.readFileSync(path.join(ROOT, "next.config.ts"), "utf8");
  assert.match(src, /output:\s*["']standalone["']/,
    "next.config.ts must set output: 'standalone' so the production image is small");
});

test("middleware injects the API key from server env only", () => {
  const src = fs.readFileSync(path.join(ROOT, "middleware.ts"), "utf8");
  assert.match(src, /process\.env\.API_KEY/,
    "middleware must read API_KEY from process.env");
  assert.doesNotMatch(src, /NEXT_PUBLIC_API_KEY/,
    "API key must NEVER be exposed via NEXT_PUBLIC_*");
});

test(".env.example documents the required vars", () => {
  const src = fs.readFileSync(path.join(ROOT, ".env.example"), "utf8");
  for (const v of ["API_KEY", "API_URL", "NEXT_PUBLIC_API_BASE_URL"]) {
    assert.match(src, new RegExp(`^${v}=`, "m"),
      `.env.example must document ${v}`);
  }
});

test("no committed .env.local", () => {
  assert.ok(!fs.existsSync(path.join(ROOT, ".env.local")),
    ".env.local must never be committed — it may contain a real API_KEY");
});
