/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The preview page fills each modal from the row button that opened it. A row action can swap
 * #page-content through HTMX, which replaces the modals, so this runs the real template script
 * against a swapped page. */
import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { claim, claimForm, postedFields, script, servePage } from "./preview_page.js";

const previewTemplate = readFileSync(
  resolve(process.cwd(), "netbox_data_import/templates/netbox_data_import/import_preview.html"),
  "utf8",
);
/* A silent extraction failure would run the tests against no code, or against a block cut short
 * at a nested IIFE, so both the match and its last handler are checked here. */
const modalWiringMatch = previewTemplate.match(/\/\* Modal wiring[\s\S]*?\n\}\(\)\);/);
if (!modalWiringMatch) {
  throw new Error(
    "import_preview.html holds no '/* Modal wiring' block that ends with '}());'. " +
      "Update this extraction, or move the wiring into a static .js file.",
  );
}
const modalWiring = modalWiringMatch[0];
if (!modalWiring.includes("setTimeout(dmSearch, 200)")) {
  throw new Error(
    "The '/* Modal wiring' block extracted from import_preview.html stops before its last " +
      "handler. A nested '}());' cut it short.",
  );
}
const bootstrapSource = readFileSync(resolve(process.cwd(), "node_modules/bootstrap/dist/js/bootstrap.js"), "utf8");

test("creating a role refreshes the page before another mapping uses its claim", async ({ page }) => {
  const start = previewTemplate.indexOf("function cmCreateRole()");
  const end = previewTemplate.indexOf("// ---- Device Match modal ----", start);
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  const createRole = previewTemplate.slice(start, end)
    .replace('{% url "plugins:netbox_data_import:quick_create_role" %}', "/create-role/");
  const posted = [];
  await page.route("**/create-role/", async route => {
    posted.push(await postedFields(route.request()));
    await route.fulfill({ json: { ok: true, name: "New role", slug: "new-role", created: true } });
  });
  const loads = await servePage(page, revision => `
    ${claimForm(revision)}<span id="revision">${revision}</span>
    <input name="csrfmiddlewaretoken" value="fixture-token">
    <input id="cm_new_role_name" value="New role"><input id="cm_new_role_slug" value="new-role">
    <input id="cm_role_slug"><div id="cm_create_role_error" style="display:none"></div>
    <div id="cm_create_role_form"></div><div id="cm_role_search_results"></div>
    <button id="create" onclick="cmCreateRole()">Create role</button>
    ${script("preview_claim.js")}<script>${createRole}</script>`);

  await page.locator("#create").click();

  await expect(page.locator("#revision")).toHaveText("5");
  expect(loads()).toBe(2);
  expect(posted).toEqual([{ name: "New role", slug: "new-role", ...claim() }]);
});

/* Only the elements the class-mapping handler touches. */
const pageContent = `
  <button type="button" id="configure-class" data-bs-toggle="modal" data-bs-target="#classMappingModal"
          data-source-class="Controller" data-initial-action="ignore">Configure class</button>
  <div class="modal" id="classMappingModal" tabindex="-1">
    <div class="modal-dialog"><div class="modal-content">
      <span id="cm_title_class"></span><span id="cm_source_class_display"></span>
      <input type="hidden" id="cm_source_class">
      <input type="radio" name="cm_action" id="cm_action_ignore">
      <input type="radio" name="cm_action" id="cm_action_role">
      <input type="radio" name="cm_action" id="cm_action_rack">
      <div id="cm_role_row"></div><input id="cm_role_slug"><div id="cm_role_search_results"></div>
      <div id="cm_rack_type_row"></div><input type="hidden" id="cm_creates_rack" value="0">
      <input type="hidden" id="cm_rack_type_id"><input id="cm_rack_type_search_q">
      <div id="cm_rack_type_search_results"></div>
      <div id="cm_rack_type_selected"><span id="cm_rack_type_selected_name"></span></div>
      <button type="button" data-bs-dismiss="modal">Close</button>
    </div></div>
  </div>
`;

async function openConfigureClass(page) {
  await page.locator("#configure-class").click();
  await expect(page.locator("#classMappingModal")).toBeVisible();
}

test("a row modal keeps its source class after an HTMX page-content swap", async ({ page }) => {
  await page.setContent(`
    <head>
      <script>${bootstrapSource}</script>
      <script>function cmToggleAction() {}</script>
    </head>
    <div id="page-content">${pageContent}</div>
  `);
  await page.addScriptTag({ content: modalWiring });

  await openConfigureClass(page);
  await expect(page.locator("#cm_source_class")).toHaveValue("Controller");
  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.locator("#classMappingModal")).toBeHidden();

  // What "Use name" does: replace #page-content, then re-run the scripts it carries.
  await page.evaluate((content) => {
    document.getElementById("page-content").innerHTML = content;
  }, pageContent);
  await page.addScriptTag({ content: modalWiring });

  await openConfigureClass(page);
  await expect(page.locator("#cm_source_class")).toHaveValue("Controller");
});

test("a modal opened with no row button leaves its form empty", async ({ page }) => {
  await page.setContent(`
    <head>
      <script>${bootstrapSource}</script>
      <script>function cmToggleAction() {}</script>
    </head>
    <div id="page-content">${pageContent}</div>
  `);
  await page.addScriptTag({ content: modalWiring });

  await page.evaluate(() => {
    window.bootstrap.Modal.getOrCreateInstance(document.getElementById("classMappingModal")).show();
  });

  await expect(page.locator("#classMappingModal")).toBeVisible();
  await expect(page.locator("#cm_source_class")).toHaveValue("");
});
