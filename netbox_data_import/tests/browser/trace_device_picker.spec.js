/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const pickerSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/trace_picker.js"),
  "utf8",
);

const fixture = `
  <base href="http://preview.test/">
  <button type="button" data-trace-device-picker="source alias" data-trace-device-label="Source Alias">Choose Device</button>
  <div class="modal" id="traceDevicePicker">
    <form id="traceDeviceForm" method="post"
          action="/plugins/data-import/trace-workspace/resolve-device/"
          data-candidates-url="/plugins/data-import/trace-workspace/device-candidates/">
      <input type="hidden" name="preview_revision" value="rev-1">
      <input type="hidden" name="search" id="traceDeviceOfferedSearch">
      <input type="hidden" name="offset" id="traceDeviceOfferedOffset">
      <input type="hidden" name="device_key" id="traceDeviceKey">
      <input type="hidden" name="device_id" id="traceDeviceId">
      <h5><span id="traceDeviceLabel"></span></h5>
      <input type="search" id="traceDeviceSearch">
      <div id="traceDeviceCount" hidden></div>
      <div id="traceDeviceError" hidden></div>
      <div class="list-group" id="traceDeviceCandidates"></div>
      <nav id="traceDevicePages" hidden>
        <button type="button" id="traceDevicePrevious">Previous</button>
        <button type="button" id="traceDeviceNext">Next</button>
      </nav>
      <button type="submit" id="traceDeviceSubmit" disabled>Save decision</button>
    </form>
  </div>
  <script>
    window.Modal = function () { this.show = function () {}; this.hide = function () {}; };
    window.Modal.getInstance = function (element) { return element.ndiModalInstance || null; };
    window.Modal.getOrCreateInstance = function (element) {
      if (!element.ndiModalInstance) element.ndiModalInstance = new window.Modal(element);
      return element.ndiModalInstance;
    };
  </script>
`;

const spares = Array.from({ length: 21 }, (_, index) => ({
  id: 200 + index,
  name: `Spare ${index + 1}`,
  display: `Spare ${index + 1}`,
  matched_facts: index === 20 ? [{ fact: "rack", source: "R1", mapped: "", netbox: "R1" }] : [],
  conflicting_facts: [],
  import_location: null,
}));

test("a Device on the second page is chosen with the search and offset that offered it", async ({ page }) => {
  const asked = [];
  await page.route("**/trace-workspace/device-candidates/**", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    asked.push([params.get("device_key"), params.get("search"), params.get("offset")]);
    const offset = Number(params.get("offset"));
    const candidates = spares.slice(offset, offset + 20);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, candidates, shown: candidates.length, total: 21, offset, limit: 20 }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: pickerSource });

  await page.locator("[data-trace-device-picker]").click();
  await expect(page.locator("#traceDeviceCount")).toHaveText("20 of 21 eligible");
  await page.locator("#traceDeviceSearch").fill("Spare");
  await expect.poll(() => asked.length).toBe(2);
  await page.locator("#traceDeviceNext").click();
  await expect(page.locator("#traceDeviceCandidates button")).toHaveText(["Spare 21Matches rack: source R1, NetBox R1"]);
  await page.locator("#traceDeviceCandidates button").click();

  expect(asked).toEqual([
    ["source alias", "", "0"],
    ["source alias", "Spare", "0"],
    ["source alias", "Spare", "20"],
  ]);
  await expect(page.locator("#traceDeviceId")).toHaveValue("220");
  await expect(page.locator("#traceDeviceOfferedSearch")).toHaveValue("Spare");
  await expect(page.locator("#traceDeviceOfferedOffset")).toHaveValue("20");
  await expect(page.locator("#traceDeviceSubmit")).toBeEnabled();
});
