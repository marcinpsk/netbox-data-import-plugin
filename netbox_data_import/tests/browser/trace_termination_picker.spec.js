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
  <script>
    /* Bootstrap's shape, so the fixture proves how the picker constructs and reuses a Modal. */
    window.ndiModalShows = [];
    window.ndiModalInstances = 0;
    window.Modal = function (element) {
      window.ndiModalInstances += 1;
      this.show = function (trigger) {
        window.ndiModalShows.push({
          connected: element.isConnected,
          trigger: trigger ? trigger.dataset.tracePicker : null,
        });
      };
    };
    window.Modal.getOrCreateInstance = function (element) {
      if (!element.ndiModalInstance) element.ndiModalInstance = new window.Modal(element);
      return element.ndiModalInstance;
    };
  </script>
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
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    // Each query answers differently, so waiting on the text proves which render is on screen.
    const search = new URL(route.request().url()).searchParams.get("search") || "";
    const candidate = search === "mgmt"
      ? { id: 9, name: "mgmt0", display: "mgmt0" }
      : { id: 1, name: "eth0", display: "eth0" };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, candidates: [candidate], shown: 1, total: 1 }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();
  await page.locator("#traceTerminationSearch").fill("mgmt");
  await expect(page.locator("#traceTerminationCandidates button")).toHaveText(["mgmt0"]);
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

test("the search that travels with a decision is the one that produced the offer", async ({ page }) => {
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    const search = new URL(route.request().url()).searchParams.get("search") || "";
    if (search === "zz") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          candidates: [{ id: 77, name: "zz-target", display: "zz-target" }],
          shown: 1,
          total: 1,
        }),
      });
      return;
    }
    // The unfiltered page is slow and does not contain the target, exactly as on a real device.
    await new Promise((done) => setTimeout(done, 3000));
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, candidates: [], shown: 0, total: 21 }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();
  await page.locator("#traceTerminationSearch").fill("zz");
  await expect(page.locator("#traceTerminationCandidates button")).toHaveText(["zz-target"]);

  // Clearing the box starts a slower unfiltered request; the offer on screen is still the zz one.
  await page.locator("#traceTerminationSearch").fill("");
  await page.locator("#traceTerminationCandidates button").first().click();

  await expect(page.locator("#traceTerminationObjectId")).toHaveValue("77");
  await expect(page.locator("#traceTerminationOfferedSearch")).toHaveValue("zz");
});

test("a refused lookup drops the candidate the previous search offered", async ({ page }) => {
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    const search = new URL(route.request().url()).searchParams.get("search") || "";
    if (search === "broken") {
      await route.fulfill({
        status: 400,
        contentType: "application/json",
        body: JSON.stringify({ ok: false, error: "That termination cannot be resolved here." }),
      });
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, candidates: [{ id: 5, name: "eth0", display: "eth0" }], shown: 1, total: 1 }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();
  await page.locator("#traceTerminationCandidates button").first().click();
  await expect(page.locator("#traceTerminationSubmit")).toBeEnabled();

  await page.locator("#traceTerminationSearch").fill("broken");

  // The offer is off the screen, so nothing is left to save.
  await expect(page.locator("#traceTerminationError")).toHaveText("That termination cannot be resolved here.");
  await expect(page.locator("#traceTerminationSubmit")).toBeDisabled();
  await expect(page.locator("#traceTerminationObjectId")).toHaveValue("");
  await expect(page.locator("#traceTerminationObjectType")).toHaveValue("");
  await expect(page.locator("#traceTerminationOfferedSearch")).toHaveValue("");
});

test("a lookup that never answers drops the offer on screen", async ({ page }) => {
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    const search = new URL(route.request().url()).searchParams.get("search") || "";
    if (search === "gone") {
      await route.abort("failed");
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, candidates: [{ id: 5, name: "eth0", display: "eth0" }], shown: 1, total: 4 }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();
  await page.locator("#traceTerminationCandidates button").first().click();
  await expect(page.locator("#traceTerminationSubmit")).toBeEnabled();

  await page.locator("#traceTerminationSearch").fill("gone");

  await expect(page.locator("#traceTerminationError")).toHaveText("The candidates could not be read.");
  await expect(page.locator("#traceTerminationCandidates button")).toHaveCount(0);
  await expect(page.locator("#traceTerminationCount")).toBeHidden();
  await expect(page.locator("#traceTerminationSubmit")).toBeDisabled();
  await expect(page.locator("#traceTerminationObjectId")).toHaveValue("");
});

test("a boosted navigation that evaluates the script again opens the picker once", async ({ page }) => {
  let requests = 0;
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    requests += 1;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, candidates: [{ id: 1, name: "eth0", display: "eth0" }], shown: 1, total: 1 }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });
  // An htmx boost swaps the page and evaluates the script the new page carries a second time.
  await page.evaluate((markup) => { document.body.innerHTML = markup; }, fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();

  await expect(page.locator("#traceTerminationCandidates button")).toHaveText(["eth0"]);
  expect(requests).toBe(1);
  // The one Modal shown has to be the one on screen, not the detached copy the swap replaced.
  expect(await page.evaluate(() => window.ndiModalShows)).toEqual([
    { connected: true, trigger: "device:DEV-A|cards:|port:absent|kind:interface|role:termination" },
  ]);
});

test("opening the picker twice reuses the one Modal the page already has", async ({ page }) => {
  await serveCandidates(page, { ok: true, candidates: [], shown: 0, total: 0 });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  await page.locator("[data-trace-picker]").click();
  await expect(page.locator("#traceTerminationCount")).toHaveText("0 of 0 eligible");
  await page.locator("[data-trace-picker]").click();

  expect(await page.evaluate(() => window.ndiModalInstances)).toBe(1);
});

test("a lookup that settles after a swap does not answer into the page that replaced it", async ({ page }) => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route("**/trace-workspace/candidates/**", async (route) => {
    await held;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ok: true, candidates: [{ id: 1, name: "stale", display: "stale" }], shown: 1, total: 1 }),
    });
  });
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });
  await page.locator("[data-trace-picker]").click();

  // The boost swaps the page while that lookup is still in flight, then the answer arrives.
  await page.evaluate((markup) => { document.body.innerHTML = markup; }, fixture);
  release();

  await expect(page.locator("#traceTerminationCount")).toBeHidden();
  await expect(page.locator("#traceTerminationCandidates button")).toHaveCount(0);
});

test("a search on a page whose picker the swap removed does not throw", async ({ page }) => {
  await serveCandidates(page, { ok: true, candidates: [], shown: 0, total: 0 });
  const failures = [];
  page.on("pageerror", (error) => failures.push(error.message));
  await page.setContent(fixture);
  await page.addScriptTag({ content: controllerSource });

  // A boost can land on a page with no picker at all, while the debounce is still pending.
  await page.evaluate(() => {
    var box = document.getElementById("traceTerminationSearch");
    box.dispatchEvent(new Event("input", { bubbles: true }));
    document.body.innerHTML = "<p>another page</p>";
  });
  await page.waitForTimeout(400);

  expect(failures).toEqual([]);
});
