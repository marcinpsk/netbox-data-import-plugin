/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const syncControllerSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/sync_row_modal.js"),
  "utf8",
);

const fixture = `
  <button id="sync-row-1" data-row-number="1" data-name="row one">Sync row one</button>
  <button id="sync-row-2" data-row-number="2" data-name="row two">Sync row two</button>
  <div id="syncRowModal" data-sync-url="/sync-single-row/">
    <span id="syncRowName"></span>
    <span id="syncRowNumber"></span>
    <span id="syncRowSourceId"></span>
    <span id="syncRowBadge"></span>
    <table><tbody id="syncRowFields"></tbody></table>
    <div class="form-check">
      <input class="form-check-input" type="checkbox" id="syncRowRecalculate" checked>
      <label class="form-check-label" for="syncRowRecalculate">Recalculate the preview after syncing</label>
    </div>
    <div id="syncRowError" class="d-none"></div>
    <button id="syncRowConfirm">
      <span class="ndi-sync-row-idle">Confirm</span>
      <span class="ndi-sync-row-loading d-none"><span class="ndi-sync-row-loading-label">Syncing</span></span>
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
    window.ndiPostPreviewAction = (url, body) =>
      new Promise((resolveRequest, rejectRequest) => {
        window.pendingSyncs.push({ url, rowNumber: body.get("row_number"), resolveRequest, rejectRequest });
      });
    window.staleDetails = [];
    window.ndiMarkPreviewStale = (detail) => {
      window.staleCount = (window.staleCount || 0) + 1;
      window.staleDetails.push(detail);
    };
    window.recalculateCount = 0;
    window.ndiRecalculatePreview = () => {
      window.recalculateCount += 1;
      return true;
    };
    window.syncModalHideCount = 0;
    window.Modal = {
      getOrCreateInstance: () => ({ hide: () => (window.syncModalHideCount += 1) }),
    };
  });
  await page.addScriptTag({ content: syncControllerSource });
}

async function confirmRow(page, buttonId) {
  await openRow(page, buttonId);
  await page.locator("#syncRowConfirm").click();
}

test("the write goes to the URL the template names", async ({ page }) => {
  await setUp(page);

  await confirmRow(page, "sync-row-1");

  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);
  expect(await page.evaluate(() => window.pendingSyncs[0].url)).toBe("/sync-single-row/");
});

test("a late sync response updates the row that submitted it", async ({ page }) => {
  await setUp(page);

  await confirmRow(page, "sync-row-1");
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
});

test("a sync response for the open row hides its modal when it does not recalculate", async ({ page }) => {
  await setUp(page);
  await page.locator("#syncRowRecalculate").uncheck();

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);

  await page.evaluate(() => {
    window.pendingSyncs[0].resolveRequest({ message: "Row synchronized." });
  });

  await expect(page.locator("#sync-row-1")).toBeDisabled();
  await expect.poll(() => page.evaluate(() => window.syncModalHideCount)).toBe(1);
});

test("a successful response without a message uses a useful tooltip", async ({ page }) => {
  await setUp(page);
  await confirmRow(page, "sync-row-1");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);

  await page.evaluate(() => {
    window.pendingSyncs[0].resolveRequest({});
  });

  await expect(page.locator("#sync-row-1")).toHaveAttribute("title", "Synced to NetBox.");
});

test("a missing row-action helper restores the controls and explains the failure", async ({ page }) => {
  await setUp(page);
  await page.evaluate(() => { window.ndiPostPreviewAction = undefined; });
  await openRow(page, "sync-row-1");

  await page.locator("#syncRowConfirm").click();

  await expect(page.locator("#sync-row-1")).toBeEnabled();
  await expect(page.locator("#syncRowConfirm")).toBeEnabled();
  await expect(page.locator("#syncRowError")).toContainText("Reload the page");
  await expect(page.locator("#syncRowError")).toBeVisible();
});

test("a missing stale helper does not turn a successful sync into a failure", async ({ page }) => {
  await setUp(page);
  await page.locator("#syncRowRecalculate").uncheck();
  await page.evaluate(() => { window.ndiMarkPreviewStale = undefined; });
  await confirmRow(page, "sync-row-1");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);

  await page.evaluate(() => {
    window.pendingSyncs[0].resolveRequest({ message: "Row synchronized." });
  });

  await expect(page.locator("#sync-row-1")).toBeDisabled();
  await expect(page.locator("#sync-row-1")).toContainText("Synced");
  await expect(page.locator("#syncRowError")).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.syncModalHideCount)).toBe(1);
});

test("a late failure leaves a newer sync request in progress", async ({ page }) => {
  await setUp(page);

  await confirmRow(page, "sync-row-1");
  await confirmRow(page, "sync-row-2");
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

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);

  await expect(page.locator("#sync-row-1")).toBeDisabled();
  await openRow(page, "sync-row-1");
  await expect(page.locator("#syncRowConfirm")).toBeDisabled();
  await page.locator("#syncRowConfirm").dispatchEvent("click");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);

  await page.evaluate(() => {
    window.pendingSyncs[0].rejectRequest(new Error("Sync failed."));
  });

  await expect(page.locator("#sync-row-1")).toBeEnabled();
});

test("a successful sync recalculates the preview instead of asking the operator to", async ({ page }) => {
  await setUp(page);

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);
  await page.evaluate(() => {
    window.pendingSyncs[0].resolveRequest({ message: "Row synchronized." });
  });

  await expect.poll(() => page.evaluate(() => window.recalculateCount)).toBe(1);
  // The page is leaving, so nothing reports a stale preview and the modal stays put.
  expect(await page.evaluate(() => window.staleCount || 0)).toBe(0);
  expect(await page.evaluate(() => window.syncModalHideCount)).toBe(0);
  await expect(page.locator(".ndi-sync-row-loading-label")).toHaveText("Recalculating preview…");
});

test("clearing the choice leaves the operator to recalculate", async ({ page }) => {
  await setUp(page);
  await page.locator("#syncRowRecalculate").uncheck();

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);
  await page.evaluate(() => {
    window.pendingSyncs[0].resolveRequest({ message: "Row synchronized." });
  });

  await expect.poll(() => page.evaluate(() => window.staleCount || 0)).toBe(1);
  expect(await page.evaluate(() => window.recalculateCount)).toBe(0);
});

test("a second sync still in flight holds the recalculation back", async ({ page }) => {
  await setUp(page);

  await confirmRow(page, "sync-row-1");
  await confirmRow(page, "sync-row-2");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(2);

  await page.evaluate(() => {
    window.pendingSyncs[0].resolveRequest({ message: "Row one synchronized." });
  });

  // Recalculating now would abandon the write row two has already sent.
  await expect.poll(() => page.evaluate(() => window.staleCount || 0)).toBe(1);
  expect(await page.evaluate(() => window.recalculateCount)).toBe(0);

  await page.evaluate(() => {
    window.pendingSyncs[1].resolveRequest({ message: "Row two synchronized." });
  });

  await expect.poll(() => page.evaluate(() => window.recalculateCount)).toBe(1);
});

test("a failed sync recalculates nothing", async ({ page }) => {
  await setUp(page);

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);
  await page.evaluate(() => {
    window.pendingSyncs[0].rejectRequest(new Error("Sync failed."));
  });

  await expect(page.locator("#syncRowError")).toContainText("Sync failed");
  expect(await page.evaluate(() => window.recalculateCount)).toBe(0);
});

test("a page without the recalculation helper still reports the stale preview", async ({ page }) => {
  await setUp(page);
  await page.evaluate(() => { window.ndiRecalculatePreview = undefined; });

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);
  await page.evaluate(() => {
    window.pendingSyncs[0].resolveRequest({ message: "Row synchronized." });
  });

  await expect.poll(() => page.evaluate(() => window.staleCount || 0)).toBe(1);
  await expect.poll(() => page.evaluate(() => window.syncModalHideCount)).toBe(1);
});

test("the stale notice names the object the sync wrote", async ({ page }) => {
  await setUp(page);
  await page.locator("#syncRowRecalculate").uncheck();

  await confirmRow(page, "sync-row-1");
  await expect.poll(() => page.evaluate(() => window.pendingSyncs.length)).toBe(1);
  await page.evaluate(() => {
    window.pendingSyncs[0].resolveRequest({
      message: "Synchronized.",
      detail: "Rack 'rack-a' was created in NetBox.",
    });
  });

  await expect.poll(() => page.evaluate(() => window.staleDetails)).toEqual([
    "Rack 'rack-a' was created in NetBox.",
  ]);
});
