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
  <table><tbody>
    <tr id="row-1"><td>
      <button class="ndi-diff-toggle" data-diff-target="diff-1" aria-expanded="true">Fields differ</button>
    </td></tr>
    <tr id="diff-1" class="ndi-diff-row show"><td>
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

test("Ignore shows pending feedback and replaces only the expanded preview row", async ({ page }) => {
  let requestCount = 0;
  await page.route("**/ignore-field-difference/", async (route) => {
    requestCount += 1;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        row_number: 1,
        row_html: '<tr id="row-1"><td>1 field(s) ignored</td></tr><tr id="diff-1" class="ndi-diff-row"><td id="ignored-field-1-u_position">Ignored</td></tr>',
      }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  const button = page.locator(".ndi-field-review-form button");
  await button.click();
  await expect(button).toBeDisabled();
  await expect(button).toContainText("Updating");
  await expect(page.locator("#row-1")).toContainText("1 field(s) ignored");

  expect(requestCount).toBe(1);
  await expect(page.locator("#diff-1")).toHaveClass(/show/);
  await expect(page.locator("#ignored-field-1-u_position")).toBeVisible();
});

test("placement sync replaces stale expanded field details", async ({ page }) => {
  await page.route("**/sync-placement/", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        row_number: 1,
        row_html: '<tr id="row-1"><td>Placement synchronized</td></tr><tr id="diff-1" class="ndi-diff-row"><td id="diff-field-1-status">Status differs</td></tr>',
      }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.getByRole("button", { name: "Sync placement" }).click();

  await expect(page.locator("#row-1")).toContainText("Placement synchronized");
  await expect(page.locator("#diff-1")).toHaveClass(/show/);
  await expect(page.locator("#diff-field-1-status")).toBeVisible();
  await expect(page.locator("#diff-field-1-u_position")).toHaveCount(0);
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
