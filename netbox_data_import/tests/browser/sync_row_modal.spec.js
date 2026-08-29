/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const previewTemplate = readFileSync(
  resolve(process.cwd(), "netbox_data_import/templates/netbox_data_import/import_preview.html"),
  "utf8",
);
const syncControllerSource = previewTemplate.match(
  /<script>\n(\(function \(\) \{\n  var modal = document\.getElementById\('syncRowModal'\);[\s\S]*?)\n<\/script>/,
)[1];

const fixture = `
  <button id="sync-row-1" data-row-number="1" data-name="row one">Sync row one</button>
  <button id="sync-row-2" data-row-number="2" data-name="row two">Sync row two</button>
  <div id="syncRowModal">
    <span id="syncRowName"></span>
    <span id="syncRowNumber"></span>
    <span id="syncRowSourceId"></span>
    <span id="syncRowBadge"></span>
    <table><tbody id="syncRowFields"></tbody></table>
    <div id="syncRowError" class="d-none"></div>
    <button id="syncRowConfirm">
      <span class="ndi-sync-row-idle">Confirm</span>
      <span class="ndi-sync-row-loading d-none">Syncing</span>
    </button>
  </div>
`;

async function openRow(page, buttonId) {
  await page.locator("#syncRowModal").evaluate((modal, id) => {
    const event = new Event("show.bs.modal");
    Object.defineProperty(event, "relatedTarget", { value: document.getElementById(id) });
    modal.dispatchEvent(event);
  }, buttonId);
}

async function setUp(page) {
  await page.setContent(fixture);
  await page.evaluate(() => {
    window.pendingSyncs = [];
    window.ndiPostPreviewAction = (_url, body) =>
      new Promise((resolveRequest, rejectRequest) => {
        window.pendingSyncs.push({ rowNumber: body.get("row_number"), resolveRequest, rejectRequest });
      });
    window.ndiMarkPreviewStale = () => {};
    window.syncModalHideCount = 0;
    window.Modal = {
      getOrCreateInstance: () => ({ hide: () => (window.syncModalHideCount += 1) }),
    };
  });
  await page.addScriptTag({ content: syncControllerSource });
}

test("a late sync response updates the row that submitted it", async ({ page }) => {
  await setUp(page);

  await openRow(page, "sync-row-1");
  await page.locator("#syncRowConfirm").click();
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);
  expect(await page.evaluate(() => window.pendingSyncs[0].rowNumber)).toBe("1");

  await openRow(page, "sync-row-2");
  await page.evaluate(() => {
    window.pendingSyncs[0].resolveRequest({ message: "Row synchronized." });
  });

  await expect(page.locator("#sync-row-1")).toBeDisabled();
  await expect(page.locator("#sync-row-1")).toContainText("Synced");
  await expect(page.locator("#sync-row-2")).toBeEnabled();
  await expect(page.locator("#sync-row-2")).toHaveText("Sync row two");
  expect(await page.evaluate(() => window.syncModalHideCount)).toBe(0);
});

test("a late failure leaves a newer sync request in progress", async ({ page }) => {
  await setUp(page);

  await openRow(page, "sync-row-1");
  await page.locator("#syncRowConfirm").click();
  await openRow(page, "sync-row-2");
  await page.locator("#syncRowConfirm").click();
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(2);

  await page.evaluate(() => {
    window.pendingSyncs[0].rejectRequest(new Error("Row one failed."));
  });

  await expect(page.locator("#syncRowConfirm")).toBeDisabled();
  await expect(page.locator(".ndi-sync-row-loading")).toBeVisible();
  await expect(page.locator("#syncRowError")).toBeHidden();
  await expect(page.locator("#sync-row-1")).toBeEnabled();
  await expect(page.locator("#sync-row-2")).toBeDisabled();
});

test("a pending sync cannot be reopened for the same row", async ({ page }) => {
  await setUp(page);

  await openRow(page, "sync-row-1");
  await page.locator("#syncRowConfirm").click();
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);

  await expect(page.locator("#sync-row-1")).toBeDisabled();

  await page.evaluate(() => {
    window.pendingSyncs[0].rejectRequest(new Error("Sync failed."));
  });

  await expect(page.locator("#sync-row-1")).toBeEnabled();
});
