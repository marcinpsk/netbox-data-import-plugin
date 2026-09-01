/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const controllerSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/preview_row_actions.js"),
  "utf8",
);
const previewTemplate = readFileSync(
  resolve(process.cwd(), "netbox_data_import/templates/netbox_data_import/import_preview.html"),
  "utf8",
);
const previewStyles = previewTemplate.match(/<style>([\s\S]*?)<\/style>/)[1];

const fixture = `
  <base href="http://preview.test/">
  <input name="csrfmiddlewaretoken" value="token">
  <input id="ndi-preview-revision" value="preview-revision">
  <div id="ndi-preview-stale" hidden>
    Saved changes are pending. Recalculate the preview before import.
    <a href="/plugins/data-import/import/preview/" class="ndi-recalculate-preview">Recalculate Preview</a>
  </div>
  <a href="/plugins/data-import/import/preview/" class="btn ndi-recalculate-preview"
     id="ndi-recalculate-preview">Recalculate Preview</a>
  <button id="ndi-run-import" type="submit">Run Import</button>
  <!-- the device-type modal form: its own control named action shadows form.action -->
  <form id="ndi-device-type-form" class="ndi-deferred-preview-form" method="post"
        action="/plugins/data-import/quick-resolve-device-type/">
    <input type="hidden" name="action" value="map">
    <button type="submit">Save mapping</button>
  </form>
  <button class="ndi-sync-row-btn" id="ndi-sync-row-1" data-ndi-modal="#syncRowModal"
          title="Create this device in NetBox now">Sync to NetBox</button>
  <button class="ndi-sync-row-btn" id="ndi-sync-row-2" disabled
          title="Resolve all conflicts before syncing">Sync to NetBox</button>
  <table><tbody>
    <tr id="row-1"><td>
      <button class="ndi-diff-toggle" data-diff-target="diff-1" aria-expanded="true">Fields differ</button>
    </td></tr>
    <!-- the field-difference row in its expanded state; the toggle has its own spec -->
    <tr id="diff-1" class="ndi-diff-row"><td>
      <form class="ndi-field-review-form" action="/ignore-field-difference/" method="post">
        <input name="row_number" value="1">
        <input name="target_field" value="u_position">
        <button type="submit">Ignore</button>
      </form>
      <button class="ndi-sync-placement-btn" data-device-id="7" data-rack-name="R1"
              data-u-position="5" data-face="front" data-row-id="1">Sync placement</button>
    </td></tr>
  </tbody></table>
`;

test("Ignore saves without recalculating and marks the preview stale", async ({ page }) => {
  let requestCount = 0;
  let requestBody = "";
  await page.route("**/ignore-field-difference/", async (route) => {
    requestCount += 1;
    requestBody = route.request().postData() || "";
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        row_number: 1,
        preview_state: "recalculation_required",
        message: "Ignored the current u_position difference.",
      }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  const button = page.locator(".ndi-field-review-form button");
  await button.click();
  await expect(button).toBeDisabled();
  await expect(button).toContainText("Updating");
  await expect(button).toContainText("Saved");

  expect(requestCount).toBe(1);
  expect(requestBody).toContain("preview-revision");
  await expect(page.locator("#diff-1")).toBeVisible();
  await expect(page.locator("#ndi-preview-stale")).toBeVisible();
  await expect(page.locator("#ndi-run-import")).toBeDisabled();
});

test("a saved row action disables every sync button until the preview is recalculated", async ({ page }) => {
  await page.route("**/ignore-field-difference/", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        row_number: 1,
        preview_state: "recalculation_required",
        message: "Ignored the current u_position difference.",
      }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator(".ndi-field-review-form button").click();
  await expect(page.locator("#ndi-preview-stale")).toBeVisible();

  // The row write needs the plan the preview no longer shows, so the server refuses it anyway.
  await expect(page.locator("#ndi-sync-row-1")).toBeDisabled();
  await expect(page.locator("#ndi-sync-row-1")).toHaveAttribute(
    "title",
    "Recalculate the preview before synchronizing a row.",
  );
  await expect(page.locator("#ndi-sync-row-2")).toHaveAttribute(
    "title",
    "Resolve all conflicts before syncing",
  );
});

test("a deferred form posts to its action attribute, not to a control that shadows it", async ({ page }) => {
  let requestedUrl = "";
  await page.route("**/*", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    requestedUrl = route.request().url();
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        row_number: 1,
        preview_state: "recalculation_required",
        message: "Mapped the device type.",
      }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  const button = page.locator("#ndi-device-type-form button");
  await button.click();

  await expect(button).toContainText("Saved");
  expect(requestedUrl).toBe("http://preview.test/plugins/data-import/quick-resolve-device-type/");
});

test("placement sync defers field-detail refresh", async ({ page }) => {
  await page.route("**/sync-placement/", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        row_number: 1,
        preview_state: "recalculation_required",
        message: "Placement synchronized.",
      }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  const button = page.locator(".ndi-sync-placement-btn");
  await button.click();

  await expect(button).toContainText("Saved");
  await expect(page.locator("#diff-1")).toBeVisible();
  await expect(page.locator("#ndi-preview-stale")).toBeVisible();
  await expect(page.locator("#ndi-run-import")).toBeDisabled();
});

test("a failed row action shows its error and clears it on retry", async ({ page }) => {
  let requestCount = 0;
  await page.route("**/ignore-field-difference/", async (route) => {
    requestCount += 1;
    await route.fulfill({
      status: requestCount === 1 ? 409 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        requestCount === 1
          ? { ok: false, error: "The field difference changed." }
          : {
              ok: true,
              row_number: 1,
              preview_state: "recalculation_required",
              message: "Ignored the current difference.",
            },
      ),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  const button = page.locator(".ndi-field-review-form button");
  await button.click();

  await expect(button).toBeEnabled();
  await expect(button).toHaveClass(/btn-danger/);
  await expect(page.locator(".ndi-row-action-error")).toHaveText("The field difference changed.");

  await button.click();

  await expect(button).toContainText("Saved");
  await expect(button).not.toHaveClass(/btn-danger/);
  await expect(page.locator(".ndi-row-action-error")).toHaveCount(0);
});

test("ignored field badge keeps readable contrast in both themes", async ({ page }) => {
  await page.setContent(`
    <style>
      :root { --tblr-dark: #1f2937; }
      .text-muted { color: #6b7280; }
      ${previewStyles}
    </style>
    <div class="text-muted">
      <button class="badge ndi-badge-ignored ndi-diff-toggle">1 field(s) ignored</button>
    </div>
  `);

  async function contrastRatio(theme) {
    await page.locator("html").evaluate((html, selectedTheme) => {
      html.setAttribute("data-bs-theme", selectedTheme);
    }, theme);
    return page.locator("button").evaluate((button) => {
      function rgb(value) {
        return value.match(/[\d.]+/g).slice(0, 3).map(Number);
      }
      function luminance(color) {
        const channels = color.map((channel) => {
          const normalized = channel / 255;
          return normalized <= 0.04045
            ? normalized / 12.92
            : ((normalized + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
      }
      const style = getComputedStyle(button);
      const foreground = luminance(rgb(style.color));
      const background = luminance(rgb(style.backgroundColor));
      return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
    });
  }

  expect(await contrastRatio("light")).toBeGreaterThanOrEqual(4.5);
  expect(await contrastRatio("dark")).toBeGreaterThanOrEqual(4.5);
});
