/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const controllerSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/contact_candidate_modal.js"),
  "utf8",
);
const tomSelectSource = readFileSync(
  resolve(process.cwd(), "node_modules/tom-select/dist/js/tom-select.complete.js"),
  "utf8",
);

const previewFixture = `
  <base href="http://preview.test/">
  <div id="contactCandidateModal">
    <form id="contactCandidateForm" data-contact-lookup-field="email" data-contact-lookup-url="/contact-lookup/">
      <input type="hidden" id="contactCandidateSourceId">
      <input type="hidden" id="contactCandidateOriginalValue">
      <input type="hidden" id="contactCandidateResolvedFields">
      <input type="hidden" id="contactCandidateContactId">
      <div id="contactCandidateSummary">
        <div id="contactCandidateSummaryName"></div>
        <div id="contactCandidateSummaryEmail"></div>
        <div id="contactCandidateSummaryPhone"></div>
        <button type="button" id="contactCandidateEditToggle" aria-expanded="false">Change</button>
      </div>
      <div id="contactCandidateProvenance"></div>
      <div id="contactCandidateSuggestion" class="d-none"></div>
      <div id="contactCandidateEdit" hidden>
        <div id="contactCandidateValueRows"></div>
        <button type="button" id="contactCandidateAddValue">Add</button>
      </div>
      <button type="button" id="contactCandidateLinkExisting" aria-expanded="false">Link</button>
      <input type="checkbox" id="contactCandidateNone">
      <div id="contactCandidateExistingWrap" hidden>
        <select id="contactCandidateExisting"></select>
      </div>
    </form>
  </div>
  <script>
    var EXISTING_RESOLUTIONS = {
      "source-saved": {
        "candidate:contact": {
          "resolved_fields": {
            "contact_resolution_applied": true,
            "contact_field_sources": { "name": "Owner" }
          }
        }
      }
    };
  </script>
  <script id="ndi-candidate-values-by-row" type="application/json">
    {"first-row":{"contact":{
       "Primary Contact":"grace.hopper@example.invalid",
       "Owner":"Lab Ops",
       "Contact":"Grace Hopper",
       "Contact Number":"+44 20 7946 0102"}},
     "saved-row":{"contact":{
       "Primary Contact":"ada@example.invalid",
       "Owner":"Ada Lovelace"}}}
  </script>
  <script id="ndi-contact-suggestions-by-row" type="application/json">{}</script>
  <script id="ndi-contact-role-suggestions-by-row" type="application/json">
    {"first-row":{"email":"Primary Contact","phone":"Contact Number","name":"Contact"},
     "saved-row":{"email":"Primary Contact"}}
  </script>
`;

/* NetBox enhances every plain <select> with its own Tom Select instance through
 * initStaticSelects() and never leaves the library on `window`. */
async function initNetBoxSelects(page) {
  await page.addScriptTag({ content: tomSelectSource });
  await page.evaluate(() => {
    for (const select of document.querySelectorAll("select:not(.tomselected)")) {
      new TomSelect(select, { create: false, maxOptions: undefined, plugins: { clear_button: {} } });
    }
    delete window.TomSelect;
  });
}

async function openRow(page, rowNumber, sourceId) {
  await page.evaluate(
    ({ rowNumber: row, sourceId: source }) => {
      const button = document.createElement("button");
      button.dataset.rowNumber = row;
      button.dataset.sourceId = source;
      const event = new Event("show.bs.modal");
      Object.defineProperty(event, "relatedTarget", { value: button });
      document.getElementById("contactCandidateModal").dispatchEvent(event);
    },
    { rowNumber, sourceId },
  );
}

async function setUp(page, fixture = previewFixture) {
  await page.setContent(fixture);
  await initNetBoxSelects(page);
  await page.addScriptTag({ content: controllerSource });
}

/** Return the {sourceColumn: role} pairs the value rows currently show. */
function rolesByColumn(page) {
  return page.evaluate(() =>
    Object.fromEntries(
      [...document.querySelectorAll("#contactCandidateValueRows .ndi-contact-value-row")].map((row) => [
        row.dataset.sourceColumn,
        row.querySelector(".ndi-contact-role").value,
      ]),
    ),
  );
}

/** Run the submit handler without letting the form navigate, and return the saved payload. */
function submitPayload(page) {
  return page.evaluate(() => {
    const form = document.getElementById("contactCandidateForm");
    const blocker = (event) => event.preventDefault();
    form.addEventListener("submit", blocker);
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    form.removeEventListener("submit", blocker);
    return JSON.parse(document.getElementById("contactCandidateResolvedFields").value || "{}");
  });
}

test("every value in the row gets a row, and the proposal fills the roles it recognizes", async ({ page }) => {
  await setUp(page);
  await openRow(page, "first-row", "source-first");

  expect(await rolesByColumn(page)).toEqual({
    "Primary Contact": "email",
    Owner: "",
    Contact: "name",
    "Contact Number": "phone",
  });
  await expect(page.locator("#contactCandidateSummaryName")).toHaveText("Grace Hopper");
  await expect(page.locator("#contactCandidateSummaryEmail")).toHaveText("grace.hopper@example.invalid");
  await expect(page.locator("#contactCandidateProvenance")).toContainText("One column is unused: Owner.");
});

test("the detail rows stay collapsed until the operator asks for them", async ({ page }) => {
  await setUp(page);
  await openRow(page, "first-row", "source-first");

  await expect(page.locator("#contactCandidateEdit")).toBeHidden();
  await expect(page.locator("#contactCandidateEditToggle")).toHaveAttribute("aria-expanded", "false");

  await page.locator("#contactCandidateEditToggle").click();
  await expect(page.locator("#contactCandidateEdit")).toBeVisible();
  await expect(page.locator("#contactCandidateEditToggle")).toHaveAttribute("aria-expanded", "true");
});

test("claiming a role releases the row that held it", async ({ page }) => {
  await setUp(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateEditToggle").click();

  await page
    .locator('.ndi-contact-value-row[data-source-column="Owner"] .ndi-contact-role')
    .selectOption("name");

  const roles = await rolesByColumn(page);
  expect(roles.Owner).toBe("name");
  expect(roles.Contact).toBe("");
  await expect(page.locator("#contactCandidateSummaryName")).toHaveText("Lab Ops");
});

test("a saved decision wins over the proposal", async ({ page }) => {
  await setUp(page);
  await openRow(page, "saved-row", "source-saved");

  // The proposal would put the address in `email`; the stored decision names Owner only.
  expect(await rolesByColumn(page)).toEqual({ "Primary Contact": "", Owner: "name" });
});

test("the submitted payload keeps the field-sources contract", async ({ page }) => {
  await setUp(page);
  await openRow(page, "first-row", "source-first");

  expect(await submitPayload(page)).toEqual({
    contact_resolution_applied: true,
    contact_field_sources: { email: "Primary Contact", name: "Contact", phone: "Contact Number" },
    contact_field_values: {},
    contact_id: null,
  });
});

test("a typed value is submitted as a field value, not a source column", async ({ page }) => {
  await setUp(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateEditToggle").click();
  await page.locator("#contactCandidateAddValue").click();
  await page.locator(".ndi-contact-value-row[data-literal] .ndi-contact-literal").fill("ada@example.invalid");
  await page.locator(".ndi-contact-value-row[data-literal] .ndi-contact-role").selectOption("email");

  const payload = await submitPayload(page);
  expect(payload.contact_field_values).toEqual({ email: "ada@example.invalid" });
  expect(payload.contact_field_sources).not.toHaveProperty("email");
});

test("no contact for this row disables the controls and clears the selection", async ({ page }) => {
  await setUp(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateEditToggle").click();

  await page.locator("#contactCandidateNone").check();
  await expect(page.locator("#contactCandidateExisting")).toBeDisabled();
  await expect(page.locator(".ndi-contact-value-row .ndi-contact-role").first()).toBeDisabled();
  await expect(page.locator("#contactCandidateSummaryName")).toHaveText("No contact for this row");

  expect(await submitPayload(page)).toEqual({
    contact_resolution_applied: true,
    contact_field_sources: {},
    contact_field_values: {},
    contact_id: null,
  });

  await page.locator("#contactCandidateNone").uncheck();
  await expect(page.locator(".ndi-contact-value-row .ndi-contact-role").first()).not.toBeDisabled();
});

test("linking an existing Contact takes over every field", async ({ page }) => {
  await page.route("**/contact-lookup/?q=*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        results: [
          { id: 73, name: "Found Contact", email: "found.contact@example.invalid", phone: "+1 202-555-0105" },
        ],
      }),
    });
  });
  await setUp(page);
  await openRow(page, "first-row", "source-first");

  await page.locator("#contactCandidateLinkExisting").click();
  await page.locator("#contactCandidateExisting + .ts-wrapper .ts-control input").fill("found.contact");
  await page.locator("#contactCandidateExisting + .ts-wrapper .ts-dropdown .option").click();

  await expect(page.locator("#contactCandidateContactId")).toHaveValue("73");
  await expect(page.locator("#contactCandidateSummaryName")).toHaveText("Found Contact");
  await expect(page.locator("#contactCandidateProvenance")).toContainText("Linked to an existing NetBox Contact.");
  // A linked Contact supplies every field, so no candidate row may still claim one.
  expect(Object.values(await rolesByColumn(page)).filter(Boolean)).toEqual([]);

  expect(await submitPayload(page)).toMatchObject({ contact_id: 73, contact_field_sources: {} });
});

test("every generated control exposes a name to assistive technology", async ({ page }) => {
  await setUp(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateEditToggle").click();
  await page.locator("#contactCandidateAddValue").click();

  for (const role of await page.locator(".ndi-contact-role").all()) {
    await expect(role).toHaveAttribute("aria-label", "Use this value as");
  }
  await expect(page.locator(".ndi-contact-literal")).toHaveAttribute("aria-label", "Contact value");
});

test("a detected NetBox Contact is offered without being applied silently", async ({ page }) => {
  const fixture = previewFixture.replace(
    '<script id="ndi-contact-suggestions-by-row" type="application/json">{}</script>',
    `<script id="ndi-contact-suggestions-by-row" type="application/json">
      {"first-row":{"id":83,"name":"Proposed Contact","email":"grace.hopper@example.invalid","phone":"+1 202-555-0106"}}
    </script>`,
  );
  await setUp(page, fixture);
  await openRow(page, "first-row", "source-first");

  await expect(page.locator("#contactCandidateSuggestion")).toBeVisible();
  await expect(page.locator("#contactCandidateExistingWrap")).toBeVisible();
  // The row's own values still stand until the operator links the Contact.
  await expect(page.locator("#contactCandidateContactId")).toHaveValue("");
  await expect(page.locator("#contactCandidateSummaryName")).toHaveText("Grace Hopper");
});
