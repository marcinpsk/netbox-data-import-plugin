/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The jsdom suite dispatches `show.bs.modal` itself. This one lets real Bootstrap raise it,
 * opened the way the preview page opens it, so the wiring between the two is covered too. */
import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const splitModal = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/split_name_modal.js"),
  "utf8",
);
const rowControls = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/preview_row_controls.js"),
  "utf8",
);
const bootstrapSource = readFileSync(resolve(process.cwd(), "node_modules/bootstrap/dist/js/bootstrap.js"), "utf8");

const SPLIT_FIELD_VALUES = { "cn-2": { device_name: "AT900 - host-900", asset_tag: "", serial: "SN900" } };

/* The elements of the split modal, named as `import_preview.html` names them. */
const pageContent = `
  <button type="button" id="trigger" data-ndi-modal="#splitNameModal" data-source-id="cn-2"
          data-source-column="device_name" data-original-value="AT900 - host-900">Split</button>
  <div class="modal" id="splitNameModal" tabindex="-1">
    <div class="modal-dialog"><div class="modal-content">
      <form id="splitForm" data-check-device-url="http://ndi.test/check-device/">
        <input type="hidden" id="res_source_id">
        <input type="hidden" id="res_source_column">
        <input type="hidden" id="res_original_value">
        <input type="hidden" id="res_resolved_fields">
        <div id="res_original_display"></div>
        <input type="text" id="res_delimiter" value=" - ">
        <div id="res_existing_notice" class="d-none"><code id="res_existing_display"></code></div>
        <div class="row g-3" id="res_parts_row"></div>
        <div id="res_conflict_alert" class="d-none"></div>
        <div id="res_duplicate_alert" class="d-none"></div>
        <div id="res_device_check" class="d-none"><small id="res_device_check_msg"></small></div>
        <button type="submit">Save</button>
      </form>
    </div></div>
  </div>
  <script type="application/json" id="ndi-split-field-values">${JSON.stringify(SPLIT_FIELD_VALUES)}</script>
`;

async function openSplitModal(page) {
  /* `setContent` leaves the page on about:blank, where a relative URL cannot resolve, so the
   * fixture names an absolute one and the reply carries the header an opaque origin needs. */
  await page.route("**/check-device/**", (route) =>
    route.fulfill({
      json: { exists: false, count: 0, url: "" },
      headers: { "access-control-allow-origin": "*" },
    }),
  );
  await page.setContent(`<head><script>${bootstrapSource}</script></head><div id="page-content">${pageContent}</div>`);
  await page.addScriptTag({ content: rowControls });
  await page.addScriptTag({ content: splitModal });
  await page.locator("#trigger").click();
  await expect(page.locator("#splitNameModal")).toBeVisible();
}

test("the row button opens the modal with one part per piece of the cell", async ({ page }) => {
  await openSplitModal(page);

  await expect(page.locator("#res_part_val_0")).toHaveValue("AT900");
  await expect(page.locator("#res_part_val_1")).toHaveValue("host-900");
  await expect(page.locator("#res_part_field_0")).toHaveValue("asset_tag");
  await expect(page.locator("#res_part_field_1")).toHaveValue("device_name");
});

test("the existence check follows the part that names the device", async ({ page }) => {
  const lookups = [];
  page.on("request", (request) => {
    if (request.url().includes("/check-device/")) lookups.push(request.url());
  });
  await openSplitModal(page);
  await expect.poll(() => lookups.some((url) => url.includes("host-900"))).toBe(true);

  await page.locator("#res_part_field_0").selectOption("device_name");
  await page.locator("#res_part_field_1").selectOption("asset_tag");

  await expect.poll(() => lookups.at(-1)).toContain("AT900");
});

test("two parts on one field block the save and say why", async ({ page }) => {
  await openSplitModal(page);
  await page.locator("#res_part_field_0").selectOption("device_name");

  // The fixture carries no stylesheet, so `d-none` is the state to read, not the geometry.
  await expect(page.locator("#res_duplicate_alert")).not.toHaveClass(/d-none/);
  await expect(page.locator('#splitNameModal button[type="submit"]')).toBeDisabled();

  await page.locator("#res_part_field_0").selectOption("asset_tag");
  await expect(page.locator("#res_duplicate_alert")).toHaveClass(/d-none/);
  await expect(page.locator('#splitNameModal button[type="submit"]')).toBeEnabled();
});
