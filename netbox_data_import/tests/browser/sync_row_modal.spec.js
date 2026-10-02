/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { claim, claimForm, postedFields, script, servePage } from "./preview_page.js";

const REPLANNED = {
  ok: true,
  row_number: 1,
  preview_state: "replanned",
  message: "Row synchronized.",
  detail: "Rack 'rack-a' was created in NetBox.",
};
const STALE = { ok: false, error: "A newer preview replaced this one.", code: "preview_stale" };

function previewPage(revision) {
  return `
    <input name="csrfmiddlewaretoken" value="token">
    ${claimForm(revision)}
    <output id="ndi-revision">${revision}</output>
    <button id="sync-row-1" class="ndi-sync-row-btn" data-row-number="1" data-name="row one">Sync row one</button>
    <button id="sync-row-2" class="ndi-sync-row-btn" data-row-number="2" data-name="row two">Sync row two</button>
    <div id="syncRowModal" data-sync-url="/plugins/data-import/sync-single-row/">
      <span id="syncRowName"></span>
      <span id="syncRowNumber"></span>
      <span id="syncRowSourceId"></span>
      <span id="syncRowBadge"></span>
      <p id="syncRowSummary"></p>
      <table>
        <thead><tr><th>Field</th><th id="syncRowCurrentHead"></th><th id="syncRowNextHead"></th></tr></thead>
        <tbody id="syncRowFields"></tbody>
      </table>
      <div class="form-check form-switch">
        <input class="form-check-input" type="checkbox" id="syncRowShowUnchanged">
        <label class="form-check-label" for="syncRowShowUnchanged">Show fields that stay the same</label>
      </div>
      <div id="syncRowError" class="d-none"></div>
      <button id="syncRowConfirm">
        <span class="ndi-sync-row-idle">Confirm</span>
        <span class="ndi-sync-row-loading d-none"><span class="ndi-sync-row-loading-label">Syncing</span></span>
      </button>
    </div>
    <style>.d-none { display: none !important; }</style>
    ${script("preview_claim.js")}
    ${script("preview_row_actions.js")}
    ${script("sync_row_modal.js")}
  `;
}

/* Holds every sync request until the test answers it, and records what each one posted. */
async function holdSyncs(page) {
  const held = [];
  await page.route("**/sync-single-row/", async (route) => {
    held.push({ route, fields: await postedFields(route.request()) });
  });
  return held;
}

async function openRow(page, buttonId) {
  await page.locator("#syncRowModal").evaluate((modal, id) => {
    const event = new Event("show.bs.modal");
    Object.defineProperty(event, "relatedTarget", { value: document.getElementById(id) });
    modal.dispatchEvent(event);
  }, buttonId);
}

async function confirmRow(page, buttonId) {
  await openRow(page, buttonId);
  await page.locator("#syncRowConfirm").click();
}

test("a sync posts the row with the page claim and reloads the replanned preview", async ({ page }) => {
  const held = await holdSyncs(page);
  const loads = await servePage(page, previewPage);

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => held.length).toBe(1);
  expect(held[0].route.request().url()).toBe("http://preview.test/plugins/data-import/sync-single-row/");
  expect(held[0].fields).toEqual({ row_number: "1", ...claim() });
  await held[0].route.fulfill({ json: REPLANNED });

  await expect(page.locator("#ndi-revision")).toHaveText("5");
  expect(loads()).toBe(2);
});

test("a stale claim refusal shows in the modal and does not reload", async ({ page }) => {
  const held = await holdSyncs(page);
  const loads = await servePage(page, previewPage);

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => held.length).toBe(1);
  await held[0].route.fulfill({ status: 409, json: STALE });

  await expect(page.locator("#syncRowError")).toHaveText(STALE.error);
  await expect(page.locator("#syncRowError")).toBeVisible();
  await expect(page.locator("#syncRowConfirm")).toBeEnabled();
  await expect(page.locator("#sync-row-1")).toBeEnabled();
  expect(loads()).toBe(1);
});

test("a sync on the reloaded preview posts the claim that page renders", async ({ page }) => {
  const held = await holdSyncs(page);
  await servePage(page, previewPage);

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => held.length).toBe(1);
  await held[0].route.fulfill({ json: REPLANNED });
  await expect(page.locator("#ndi-revision")).toHaveText("5");

  await confirmRow(page, "sync-row-2");
  await expect.poll(() => held.length).toBe(2);
  expect(held[1].fields).toEqual({ row_number: "2", ...claim(5) });
});

test("a late failure leaves a newer sync request in progress", async ({ page }) => {
  const held = await holdSyncs(page);
  await servePage(page, previewPage);

  await confirmRow(page, "sync-row-1");
  await confirmRow(page, "sync-row-2");
  await expect.poll(() => held.length).toBe(2);

  await held[0].route.fulfill({ status: 409, json: STALE });

  await expect(page.locator("#sync-row-1")).toBeEnabled();
  await expect(page.locator("#syncRowConfirm")).toBeDisabled();
  await expect(page.locator(".ndi-sync-row-loading")).toBeVisible();
  await expect(page.locator("#syncRowError")).toBeHidden();
  await expect(page.locator("#sync-row-2")).toBeDisabled();
});

test("a pending sync cannot be reopened for the same row", async ({ page }) => {
  const held = await holdSyncs(page);
  await servePage(page, previewPage);

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => held.length).toBe(1);

  await expect(page.locator("#sync-row-1")).toBeDisabled();
  await openRow(page, "sync-row-1");
  await expect(page.locator("#syncRowConfirm")).toBeDisabled();
  await page.locator("#syncRowConfirm").dispatchEvent("click");
  await page.waitForTimeout(100);
  expect(held).toHaveLength(1);

  await held[0].route.fulfill({ status: 409, json: STALE });

  await expect(page.locator("#sync-row-1")).toBeEnabled();
});

test("the modal reports the reload while the replanned preview loads", async ({ page }) => {
  const held = await holdSyncs(page);
  await servePage(page, previewPage);
  // The old page is gone once the reload lands, so it records its last state as it unloads.
  await page.evaluate(() => {
    window.addEventListener("beforeunload", () => {
      window.sessionStorage.setItem("leaving", JSON.stringify({
        label: document.querySelector(".ndi-sync-row-loading-label").textContent,
        confirmDisabled: document.getElementById("syncRowConfirm").disabled,
      }));
    });
  });

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => held.length).toBe(1);
  await held[0].route.fulfill({ json: REPLANNED });

  await expect(page.locator("#ndi-revision")).toHaveText("5");
  expect(await page.evaluate(() => JSON.parse(window.sessionStorage.getItem("leaving")))).toEqual({
    label: "Reloading preview…",
    confirmDisabled: true,
  });
});

test("a missing row-action helper restores the controls and explains the failure", async ({ page }) => {
  await servePage(page, previewPage);
  await page.evaluate(() => { window.ndiPostPreviewAction = undefined; });
  await openRow(page, "sync-row-1");

  await page.locator("#syncRowConfirm").click();

  await expect(page.locator("#sync-row-1")).toBeEnabled();
  await expect(page.locator("#syncRowConfirm")).toBeEnabled();
  await expect(page.locator("#syncRowError")).toContainText("Reload the page");
  await expect(page.locator("#syncRowError")).toBeVisible();
});
