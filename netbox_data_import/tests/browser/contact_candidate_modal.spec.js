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
      <input type="checkbox" id="contactCandidateNone">
      <div id="contactCandidateFields">
        <div id="contactCandidateSuggestion" class="d-none"></div>
        <label>Existing NetBox Contact<select id="contactCandidateExisting"></select></label>
        <label>Contact name<select id="contactCandidateName"></select><input id="contactCandidateNameValue"></label>
        <label>Email address<select id="contactCandidateEmail"></select><input id="contactCandidateEmailValue"></label>
        <label>Phone number<select id="contactCandidatePhone"></select><input id="contactCandidatePhoneValue"></label>
      </div>
    </form>
  </div>
  <script>
    var EXISTING_RESOLUTIONS = {
      "source-first": {
        "candidate:contact": {
          "resolved_fields": {
            "contact_resolution_applied": true,
            "contact_field_sources": {
              "name": "Contact Name",
              "email": "Contact Email"
            }
          }
        }
      }
    };
  </script>
  <script id="ndi-candidate-values-by-row" type="application/json">
    {"first-row":{"contact":{"Contact Name":"First Contact","Contact Email":"first@example.invalid"}},
     "second-row":{"contact":{"Owner Name":"Second Contact","Owner Email":"second@example.invalid"}}}
  </script>
  <script id="ndi-contact-suggestions-by-row" type="application/json">{}</script>
`;

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

test("contact candidate fields stay visible and synchronized in the browser", async ({ page }) => {
  await page.setContent(previewFixture);
  await page.addScriptTag({ content: tomSelectSource });
  await page.evaluate(() => {
    for (const id of ["contactCandidateName", "contactCandidateEmail", "contactCandidatePhone"]) {
      new TomSelect(document.getElementById(id), { create: false });
    }
  });
  await page.addScriptTag({ content: controllerSource });

  await openRow(page, "first-row", "source-first");

  const name = page.locator("#contactCandidateName");
  const email = page.locator("#contactCandidateEmail");
  const nameWrapper = page.locator("#contactCandidateName + .ts-wrapper");
  const emailWrapper = page.locator("#contactCandidateEmail + .ts-wrapper");
  await expect(name).toHaveValue("Contact Name");
  await expect(email).toHaveValue("Contact Email");
  await expect(nameWrapper.locator(".ts-control .item")).toHaveText("Contact Name: First Contact");
  await expect(emailWrapper.locator(".ts-control .item")).toHaveText("Contact Email: first@example.invalid");

  await nameWrapper.locator(".ts-control").click();
  const options = nameWrapper.locator(".ts-dropdown .option");
  await expect(options).toHaveCount(2);
  await expect(options).toHaveText(["Contact Name: First Contact", "Contact Email: first@example.invalid"]);
  await nameWrapper.locator(".ts-control").click();

  await openRow(page, "second-row", "source-second");
  await expect(nameWrapper.locator(".ts-control .item")).toHaveCount(0);
  await nameWrapper.locator(".ts-control").click();
  await expect(nameWrapper.locator(".ts-dropdown .option")).toHaveText([
    "Owner Name: Second Contact",
    "Owner Email: second@example.invalid",
  ]);

  const checkbox = page.locator("#contactCandidateNone");
  await checkbox.check();
  for (const selector of ["#contactCandidateName", "#contactCandidateEmail", "#contactCandidatePhone"]) {
    await expect(page.locator(selector)).toBeDisabled();
    await expect(page.locator(`${selector} + .ts-wrapper`)).toHaveClass(/(^|\s)disabled(\s|$)/);
  }

  await checkbox.uncheck();
  for (const selector of ["#contactCandidateName", "#contactCandidateEmail", "#contactCandidatePhone"]) {
    await expect(page.locator(selector)).not.toBeDisabled();
    await expect(page.locator(`${selector} + .ts-wrapper`)).not.toHaveClass(/(^|\s)disabled(\s|$)/);
  }
});

test("contact picker searches NetBox and copies the selected Contact details", async ({ page }) => {
  await page.route("**/contact-lookup/?q=*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        results: [{
          id: 73,
          name: "Found Contact",
          email: "found.contact@example.invalid",
          phone: "+1 202-555-0105",
        }],
      }),
    });
  });
  await page.setContent(previewFixture);
  await page.addScriptTag({ content: tomSelectSource });
  await page.evaluate(() => {
    for (const id of ["contactCandidateName", "contactCandidateEmail", "contactCandidatePhone"]) {
      new TomSelect(document.getElementById(id), { create: false });
    }
  });
  await page.addScriptTag({ content: controllerSource });
  await openRow(page, "first-row", "source-first");

  const pickerInput = page.locator("#contactCandidateExisting + .ts-wrapper .ts-control input");
  await pickerInput.fill("found.contact");
  await expect(page.locator("#contactCandidateExisting + .ts-wrapper .ts-dropdown .option")).toContainText(
    "Found Contact",
  );
  await page.locator("#contactCandidateExisting + .ts-wrapper .ts-dropdown .option").click();

  await expect(page.locator("#contactCandidateContactId")).toHaveValue("73");
  await expect(page.locator("#contactCandidateNameValue")).toHaveValue("Found Contact");
  await expect(page.locator("#contactCandidateEmailValue")).toHaveValue("found.contact@example.invalid");
  await expect(page.locator("#contactCandidatePhoneValue")).toHaveValue("+1 202-555-0105");
});

test("detected NetBox Contact is proposed in the picker", async ({ page }) => {
  const fixture = previewFixture.replace(
    '<script id="ndi-contact-suggestions-by-row" type="application/json">{}</script>',
    `<script id="ndi-contact-suggestions-by-row" type="application/json">
      {"second-row":{"id":83,"name":"Proposed Contact","email":"second@example.invalid","phone":"+1 202-555-0106"}}
    </script>`,
  );
  await page.setContent(fixture);
  await page.addScriptTag({ content: tomSelectSource });
  await page.evaluate(() => {
    for (const id of ["contactCandidateName", "contactCandidateEmail", "contactCandidatePhone"]) {
      new TomSelect(document.getElementById(id), { create: false });
    }
  });
  await page.addScriptTag({ content: controllerSource });

  await openRow(page, "second-row", "source-second");

  await expect(page.locator("#contactCandidateExisting + .ts-wrapper .ts-control .item")).toContainText(
    "Proposed Contact · second@example.invalid · +1 202-555-0106",
  );
  await expect(page.locator("#contactCandidateContactId")).toHaveValue("83");
  await expect(page.locator("#contactCandidateNameValue")).toHaveValue("Proposed Contact");
  await expect(page.locator("#contactCandidateEmailValue")).toHaveValue("second@example.invalid");
});

test("detected Contact can be replaced through picker search", async ({ page }) => {
  await page.route("**/contact-lookup/?q=*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        results: [{
          id: 84,
          name: "Replacement Contact",
          email: "replacement@example.invalid",
          phone: "+1 202-555-0107",
        }],
      }),
    });
  });
  const fixture = previewFixture.replace(
    '<script id="ndi-contact-suggestions-by-row" type="application/json">{}</script>',
    `<script id="ndi-contact-suggestions-by-row" type="application/json">
      {"second-row":{"id":83,"name":"Proposed Contact","email":"second@example.invalid","phone":"+1 202-555-0106"}}
    </script>`,
  );
  await page.setContent(fixture);
  await page.addScriptTag({ content: tomSelectSource });
  await page.evaluate(() => {
    for (const id of ["contactCandidateName", "contactCandidateEmail", "contactCandidatePhone"]) {
      new TomSelect(document.getElementById(id), { create: false });
    }
  });
  await page.addScriptTag({ content: controllerSource });
  await openRow(page, "second-row", "source-second");

  const picker = page.locator("#contactCandidateExisting + .ts-wrapper");
  await picker.locator(".remove").click();
  await picker.locator(".ts-control input").fill("replacement");
  await expect(picker.locator(".ts-dropdown .option")).toContainText("Replacement Contact");
  await picker.locator(".ts-dropdown .option").click();

  await expect(page.locator("#contactCandidateContactId")).toHaveValue("84");
  await expect(page.locator("#contactCandidateEmailValue")).toHaveValue("replacement@example.invalid");
});
