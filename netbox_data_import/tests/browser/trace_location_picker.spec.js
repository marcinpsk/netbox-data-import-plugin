/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const searchSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/trace_picker_search.js"),
  "utf8",
);
const controllerSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/trace_location_picker.js"),
  "utf8",
);

const fixture = `
  <base href="http://preview.test/">
  <table>
    <tr><td><button type="button" data-trace-location-picker="region >> dh4"
                    data-trace-location-label="Region >> DH4">Choose Location</button></td></tr>
    <tr><td><button type="button" data-trace-location-picker="region >> dh5"
                    data-trace-location-label="Region >> DH5">Choose Location</button></td></tr>
  </table>
  <div class="modal" id="traceLocationPicker">
    <form id="traceLocationForm" method="post"
          action="/plugins/data-import/trace-workspace/location-mapping/"
          data-candidates-url="/plugins/data-import/trace-workspace/location-candidates/">
      <input type="hidden" name="preview_revision" value="rev-1">
      <input type="hidden" name="location_key" id="traceLocationKey">
      <input type="hidden" name="location_id" id="traceLocationId">
      <h5><span id="traceLocationLabel"></span></h5>
      <input type="search" id="traceLocationSearch">
      <div id="traceLocationCount" hidden></div>
      <div id="traceLocationError" hidden></div>
      <div class="list-group" id="traceLocationCandidates"></div>
      <button type="submit" id="traceLocationSubmit" disabled>Save mapping</button>
    </form>
  </div>
  <script>
    /* Bootstrap's shape, so the fixture proves how the picker constructs and reuses a Modal. */
    window.ndiModalShows = [];
    window.ndiModalHides = 0;
    window.ndiModalInstances = 0;
    window.Modal = function (element) {
      window.ndiModalInstances += 1;
      this.show = function (trigger) {
        window.ndiModalShows.push(trigger ? trigger.dataset.traceLocationPicker : null);
      };
      this.hide = function () { window.ndiModalHides += 1; };
    };
    window.Modal.getInstance = function (element) {
      return element.ndiModalInstance || null;
    };
    window.Modal.getOrCreateInstance = function (element) {
      if (!element.ndiModalInstance) element.ndiModalInstance = new window.Modal(element);
      return element.ndiModalInstance;
    };
  </script>
`;

async function servedPage(page, payload, asked = []) {
  await page.route("**/trace-workspace/location-candidates/**", async (route) => {
    asked.push(new URL(route.request().url()).searchParams.get("location_key"));
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: searchSource });
  await page.addScriptTag({ content: controllerSource });
}

const offer = {
  ok: true,
  candidates: [
    { id: 4, name: "DH4", parent: "1st Floor" },
    { id: 5, name: "DH5", parent: "1st Floor" },
  ],
  shown: 2,
  total: 7,
};

test("every row opens the one shared dialog for its own path", async ({ page }) => {
  const asked = [];
  await servedPage(page, offer, asked);

  await page.locator("[data-trace-location-picker]").nth(0).click();
  await expect(page.locator("#traceLocationCount")).toHaveText("2 of 7 visible Locations");
  await page.locator("[data-trace-location-picker]").nth(1).click();
  await expect(page.locator("#traceLocationLabel")).toHaveText("Region >> DH5");

  expect(asked).toEqual(["region >> dh4", "region >> dh5"]);
  await expect(page.locator("#traceLocationKey")).toHaveValue("region >> dh5");
  expect(await page.evaluate(() => window.ndiModalInstances)).toBe(1);
  expect(await page.evaluate(() => window.ndiModalShows)).toEqual(["region >> dh4", "region >> dh5"]);
});

test("saving waits for a chosen Location and posts that Location", async ({ page }) => {
  await servedPage(page, offer);

  await page.locator("[data-trace-location-picker]").first().click();
  await expect(page.locator("#traceLocationSubmit")).toBeDisabled();
  await expect(page.locator("#traceLocationCandidates button")).toHaveText(["DH4In 1st Floor", "DH5In 1st Floor"]);
  await page.locator("#traceLocationCandidates button").nth(1).click();

  await expect(page.locator("#traceLocationId")).toHaveValue("5");
  await expect(page.locator("#traceLocationSubmit")).toBeEnabled();
});

test("a search reaches the server and replaces the offer", async ({ page }) => {
  const searched = [];
  await page.route("**/trace-workspace/location-candidates/**", async (route) => {
    const search = new URL(route.request().url()).searchParams.get("search");
    searched.push(search);
    const candidates = search ? [{ id: 4, name: "DH4", parent: "" }] : offer.candidates;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, candidates, shown: candidates.length, total: candidates.length }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: searchSource });
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-location-picker]").first().click();
  await expect(page.locator("#traceLocationCandidates button")).toHaveCount(2);
  await page.locator("#traceLocationSearch").fill("dh4");

  await expect(page.locator("#traceLocationCandidates button")).toHaveText(["DH4"]);
  expect(searched).toEqual(["", "dh4"]);
});

test("saving closes the dialog before the swap takes it away", async ({ page }) => {
  await servedPage(page, offer);
  await page.locator("[data-trace-location-picker]").first().click();

  // htmx swaps the content the dialog lives in, so a dialog left open strands its backdrop.
  await page.evaluate(() => {
    document.getElementById("traceLocationForm").dispatchEvent(new Event("htmx:beforeRequest", { bubbles: true }));
  });

  expect(await page.evaluate(() => window.ndiModalHides)).toBe(1);
});
