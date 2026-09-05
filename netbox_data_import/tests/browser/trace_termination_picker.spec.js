/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const controllerSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/trace_termination_picker.js"),
  "utf8",
);

const fixture = `
  <base href="http://preview.test/">
  <button type="button" data-trace-picker="device:DEV-A|cards:|port:absent|kind:interface|role:termination"
          data-trace-kind="interface" data-trace-label="DEV-A absent-port">Choose termination</button>
  <div class="modal" id="traceTerminationPicker">
    <form id="traceTerminationForm" method="post"
          action="/plugins/data-import/trace-workspace/resolve-termination/"
          data-candidates-url="/plugins/data-import/trace-workspace/candidates/">
      <input type="hidden" name="preview_revision" value="rev-1">
      <input type="hidden" name="search" id="traceTerminationOfferedSearch">
      <input type="hidden" name="field_key" id="traceTerminationFieldKey">
      <input type="hidden" name="object_type" id="traceTerminationObjectType">
      <input type="hidden" name="object_id" id="traceTerminationObjectId">
      <h5><span id="traceTerminationLabel"></span></h5>
      <input type="search" id="traceTerminationSearch">
      <div id="traceTerminationCount" hidden></div>
      <div id="traceTerminationError" hidden></div>
      <div class="list-group" id="traceTerminationCandidates"></div>
      <button type="submit" id="traceTerminationSubmit" disabled>Save decision</button>
    </form>
  </div>
  <script>window.Modal = function () { return { show: function () {} }; };</script>
`;

async function serveCandidates(page, payload) {
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(payload) });
  });
}

test("the picker states how many of the eligible terminations it shows", async ({ page }) => {
  await serveCandidates(page, {
    ok: true,
    candidates: [
      { id: 1, name: "eth0", display: "eth0" },
      { id: 2, name: "eth1", display: "eth1" },
    ],
    shown: 2,
    total: 7,
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();

  await expect(page.locator("#traceTerminationCount")).toHaveText("2 of 7 eligible");
  await expect(page.locator("#traceTerminationLabel")).toHaveText("DEV-A absent-port");
  await expect(page.locator("#traceTerminationCandidates button")).toHaveCount(2);
});

test("saving is refused until a candidate is chosen", async ({ page }) => {
  await serveCandidates(page, {
    ok: true,
    candidates: [{ id: 42, name: "eth0", display: "eth0" }],
    shown: 1,
    total: 1,
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();
  await expect(page.locator("#traceTerminationSubmit")).toBeDisabled();

  await page.locator("#traceTerminationCandidates button").first().click();

  await expect(page.locator("#traceTerminationSubmit")).toBeEnabled();
  await expect(page.locator("#traceTerminationObjectId")).toHaveValue("42");
  await expect(page.locator("#traceTerminationObjectType")).toHaveValue("dcim.interface");
});

test("the picker sends the preview revision, which the server checks before it answers", async ({ page }) => {
  let asked = "";
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    asked = route.request().url();
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, candidates: [], shown: 0, total: 0 }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();

  await expect(page.locator("#traceTerminationCount")).toHaveText("0 of 0 eligible");
  expect(new URL(asked).searchParams.get("preview_revision")).toBe("rev-1");
});

test("the search that produced the offer travels with the saved decision", async ({ page }) => {
  await serveCandidates(page, {
    ok: true,
    candidates: [{ id: 9, name: "mgmt0", display: "mgmt0" }],
    shown: 1,
    total: 1,
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();
  await page.locator("#traceTerminationSearch").fill("mgmt");
  await expect(page.locator("#traceTerminationCandidates button")).toHaveCount(1);
  await page.locator("#traceTerminationCandidates button").first().click();

  await expect(page.locator("#traceTerminationOfferedSearch")).toHaveValue("mgmt");
});

test("a refused candidate query reports its reason and offers nothing", async ({ page }) => {
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    await route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, error: "That termination cannot be resolved here." }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();

  await expect(page.locator("#traceTerminationError")).toHaveText("That termination cannot be resolved here.");
  await expect(page.locator("#traceTerminationCandidates button")).toHaveCount(0);
  await expect(page.locator("#traceTerminationSubmit")).toBeDisabled();
});

test("a slower earlier search does not overwrite the answer to a later one", async ({ page }) => {
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    const url = new URL(route.request().url());
    const search = url.searchParams.get("search") || "";
    const body = search === "mgmt"
      ? { ok: true, candidates: [{ id: 9, name: "mgmt0", display: "mgmt0" }], shown: 1, total: 1 }
      : { ok: true, candidates: [{ id: 1, name: "eth0", display: "eth0" }], shown: 1, total: 5 };
    if (search !== "mgmt") {
      await new Promise((done) => setTimeout(done, 400));
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();
  await page.locator("#traceTerminationSearch").fill("mgmt");

  await expect(page.locator("#traceTerminationCount")).toHaveText("1 of 1 eligible");
  await expect(page.locator("#traceTerminationCandidates button")).toHaveText(["mgmt0"]);
});
