/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const controllerSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/preview_row_controls.js"),
  "utf8",
);
const bootstrapSource = readFileSync(resolve(process.cwd(), "node_modules/bootstrap/dist/js/bootstrap.js"), "utf8");
const bootstrapStyles = readFileSync(resolve(process.cwd(), "node_modules/bootstrap/dist/css/bootstrap.css"), "utf8");

/* The page loads this script from the head, before the rows exist, and carries no plugin
 * stylesheet here: the collapsed state must hold on the `hidden` attribute alone. */
const previewFixture = `
  <head><script>${controllerSource}</script></head>
  <table><tbody id="previewRowsBody">
    <tr data-action="update">
      <td>field-review-device</td>
      <td>
        <button type="button" class="ndi-diff-toggle" data-diff-target="diff-1" aria-expanded="false">
          <i class="mdi mdi-chevron-down"></i> 2 field(s) differ
        </button>
      </td>
    </tr>
    <tr id="diff-1" class="ndi-diff-row" hidden><td colspan="2">serial FIELD-REVIEW-SERIAL</td></tr>
  </tbody></table>
`;

test("field differences stay collapsed until the toggle is pressed", async ({ page }) => {
  await page.setContent(previewFixture);

  const diffRow = page.locator("#diff-1");
  const toggle = page.locator(".ndi-diff-toggle");
  await expect(diffRow).toBeHidden();

  await toggle.click();
  await expect(diffRow).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");

  await toggle.click();
  await expect(diffRow).toBeHidden();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
});

/* NetBox constructs one Bootstrap Modal per `data-bs-toggle="modal"` trigger at load, which
 * costs seconds on a large preview, so row buttons open their modal from the plugin instead. */
test("a row button opens its modal and reports itself as the related target", async ({ page }) => {
  await page.setContent(`
    <head><style>${bootstrapStyles}</style><script>${bootstrapSource}</script><script>${controllerSource}</script></head>
    <table><tbody id="previewRowsBody">
      <tr><td>
        <button type="button" data-ndi-modal="#conflictModal" data-row-number="4">2 conflicts</button>
      </td></tr>
    </tbody></table>
    <div class="modal" id="conflictModal" tabindex="-1">
      <div class="modal-dialog"><div class="modal-content">
        <p id="conflict-row"></p>
        <button type="button" data-bs-dismiss="modal">Close</button>
      </div></div>
    </div>
  `);
  await page.evaluate(() => {
    window.Modal = window.bootstrap.Modal;
    document.getElementById("conflictModal").addEventListener("show.bs.modal", (event) => {
      document.getElementById("conflict-row").textContent = `row ${event.relatedTarget.dataset.rowNumber}`;
    });
  });

  await expect(page.locator("#conflictModal")).toBeHidden();

  await page.getByRole("button", { name: "2 conflicts" }).click();

  await expect(page.locator("#conflictModal")).toBeVisible();
  await expect(page.locator("#conflict-row")).toHaveText("row 4");

  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.locator("#conflictModal")).toBeHidden();
  await expect(page.getByRole("button", { name: "2 conflicts" })).toBeFocused();
});
