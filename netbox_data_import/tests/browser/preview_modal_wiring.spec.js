/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The preview page fills each modal from the row button that opened it. A row action can swap
 * #page-content through HTMX, which replaces the modals, so this runs the real template script
 * against a swapped page. */
import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

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

/* Only the elements the class-mapping handler touches. */
const pageContent = `
  <button type="button" id="configure-class" data-bs-toggle="modal" data-bs-target="#classMappingModal"
          data-source-class="Controller" data-profile-id="7" data-initial-action="ignore">Configure class</button>
  <div class="modal" id="classMappingModal" tabindex="-1">
    <div class="modal-dialog"><div class="modal-content">
      <span id="cm_title_class"></span><span id="cm_source_class_display"></span>
      <input type="hidden" id="cm_profile_id"><input type="hidden" id="cm_source_class">
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

test("a row modal keeps its profile after an HTMX page-content swap", async ({ page }) => {
  await page.setContent(`
    <head>
      <script>${bootstrapSource}</script>
      <script>function cmToggleAction() {}</script>
    </head>
    <div id="page-content">${pageContent}</div>
  `);
  await page.addScriptTag({ content: modalWiring });

  await openConfigureClass(page);
  await expect(page.locator("#cm_profile_id")).toHaveValue("7");
  await expect(page.locator("#cm_source_class")).toHaveValue("Controller");
  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.locator("#classMappingModal")).toBeHidden();

  // What "Use name" does: replace #page-content, then re-run the scripts it carries.
  await page.evaluate((content) => {
    document.getElementById("page-content").innerHTML = content;
  }, pageContent);
  await page.addScriptTag({ content: modalWiring });

  await openConfigureClass(page);

  await expect(page.locator("#cm_profile_id")).toHaveValue("7");
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
  await expect(page.locator("#cm_profile_id")).toHaveValue("");
});
