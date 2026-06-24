/**
 * Browser walkthrough e2e — drives a real Chromium through every staff page and
 * the drip-editor flow, capturing console errors, uncaught exceptions, failed
 * API calls, the nav-overlap regression, and screenshots. Exits non-zero if any
 * hard finding is seen, so it can gate CI.
 *
 * This exists because unit/component tests pass in jsdom while the live app can
 * still be broken (e.g. the drip send-test 503 and the nav overlapping the
 * brand both shipped green and were only caught by clicking through the app).
 *
 * Run against a LOCAL stack (web + API + seeded DB), with the app in dev mode so
 * it auto-auths as `dev-local` (no Entra). See apps/web/e2e/README.md.
 *
 *   E2E_BASE_URL   web app base (default http://localhost:3000)
 *   E2E_API_URL    API base for ID discovery (default http://localhost:8000)
 *   E2E_SHOTS_DIR  screenshot output dir (default ./e2e-shots)
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const SHOTS = process.env.E2E_SHOTS_DIR ?? "e2e-shots";
const AUTH = { Authorization: "Bearer dev-local" };
mkdirSync(SHOTS, { recursive: true });

const findings = [];
const HARD = new Set(["UNCAUGHT", "API5XX", "NAV", "BUG"]);
function note(page, sev, msg) {
  findings.push({ page, sev, msg });
  console.log(`[${sev}] ${page}: ${msg}`);
}

async function apiJson(path) {
  try {
    const r = await fetch(`${API}${path}`, { headers: AUTH });
    return r.ok ? await r.json() : null;
  } catch {
    return null;
  }
}

// Discover real IDs so the walkthrough works against any seeded env.
const campaigns = await apiJson("/v1/drips/campaigns");
const campaignId = campaigns?.items?.[0]?.id ?? null;
const contacts = await apiJson("/v1/contacts?limit=1");
const contactId = (contacts?.items ?? contacts ?? [])[0]?.id ?? null;
const queue = await apiJson("/v1/matches/queue");
const matchId = queue?.items?.[0]?.id ?? null;

const routes = [
  ["home", "/"],
  ["contacts", "/contacts"],
  ["contacts-new", "/contacts/new"],
  ["adopters", "/adopters"],
  ["facilitators", "/facilitators"],
  ["facilitator-portal", "/facilitator"],
  ["matches", "/matches"],
  ["campaigns", "/campaigns"],
  ["admin-users", "/admin/users"],
  ["admin-suppression", "/admin/suppression"],
  contactId && ["contact-detail", `/contacts/${contactId}`],
  contactId && ["workflow", `/workflow/${contactId}`],
  matchId && ["match-detail", `/matches/${matchId}`],
  campaignId && ["campaign-detail", `/campaigns/${campaignId}`],
].filter(Boolean);

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });

for (const [name, path] of routes) {
  const page = await ctx.newPage();
  const consoleErrs = [];
  page.on("console", (m) => m.type() === "error" && consoleErrs.push(m.text()));
  page.on("pageerror", (e) => note(name, "UNCAUGHT", e.message));
  page.on("response", (r) => {
    if (r.url().includes("/api/") && r.status() >= 500)
      note(name, "API5XX", `${r.status()} ${r.request().method()} ${r.url().replace(BASE, "")}`);
  });
  try {
    const resp = await page.goto(`${BASE}${path}`, { waitUntil: "networkidle", timeout: 30000 });
    if (resp && resp.status() >= 400) note(name, "HTTP", `page returned ${resp.status()}`);
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
  } catch (e) {
    note(name, "NAV", `navigation failed: ${e.message}`);
  }
  for (const c of consoleErrs) note(name, "CONSOLE", c);
  await page.close();
}

// Nav must not overlap the brand (regression: the right-aligned nav overflowed
// leftward over the "Staff console" wordmark on ≤1280px).
{
  const page = await ctx.newPage();
  await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
  const label = await page.locator("text=Staff console").first().boundingBox().catch(() => null);
  const firstNav = await page.locator("nav[aria-label='Primary'] a").first().boundingBox().catch(() => null);
  if (label && firstNav) {
    const sameRow = Math.abs(firstNav.y - label.y) < 20;
    const overlap = firstNav.x - (label.x + label.width);
    if (sameRow && overlap < 0) note("nav", "NAV", `nav overlaps brand by ${(-overlap).toFixed(0)}px`);
  }
  await page.close();
}

// Drip editor: open a step, type, insert a token, send a test.
if (campaignId) {
  const name = "drip-editor";
  const page = await ctx.newPage();
  page.on("pageerror", (e) => note(name, "UNCAUGHT", e.message));
  try {
    await page.goto(`${BASE}/campaigns/${campaignId}`, { waitUntil: "networkidle" });
    const edits = page.getByRole("button", { name: "Edit" });
    if ((await edits.count()) === 0) note(name, "UI", "no Edit button on a campaign with steps");
    else {
      await edits.last().click();
      await page.waitForTimeout(1500);
      if ((await page.getByRole("button", { name: "Bold" }).count()) === 0)
        note(name, "BUG", "rich-text editor toolbar did not appear");
      const pm = page.locator(".ProseMirror").first();
      if (await pm.count()) { await pm.click(); await pm.type(" e2e "); }
      const insert = page.getByRole("button", { name: "Recipient name" });
      if (await insert.count()) await insert.first().click();
      const sendTest = page.getByRole("button", { name: /Send test/i });
      if (await sendTest.count()) {
        await sendTest.first().click();
        await page.waitForTimeout(2500);
        const msg = await page.locator("body").innerText();
        if (/body stream already read|worker_unavailable/i.test(msg))
          note(name, "BUG", `send-test error: ${msg.match(/(body stream already read|worker_unavailable)/i)?.[0]}`);
      } else note(name, "UI", "'Send test' button not found");
      await page.screenshot({ path: `${SHOTS}/drip-editor.png`, fullPage: true });
    }
  } catch (e) { note(name, "NAV", `flow failed: ${e.message}`); }
  await page.close();
}

await browser.close();

const bySev = findings.reduce((a, f) => ((a[f.sev] = (a[f.sev] || 0) + 1), a), {});
writeFileSync(`${SHOTS}/findings.json`, JSON.stringify(findings, null, 2));
console.log("\n===== SUMMARY =====", JSON.stringify(bySev));
const hard = findings.filter((f) => HARD.has(f.sev));
if (hard.length) {
  console.error(`\n${hard.length} hard finding(s) — failing.`);
  process.exit(1);
}
console.log("No hard findings.");
