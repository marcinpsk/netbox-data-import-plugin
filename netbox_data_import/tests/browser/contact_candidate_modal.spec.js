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
  <div id="contactCandidateModal">
    <form id="contactCandidateForm" data-contact-lookup-field="email">
      <input type="hidden" id="contactCandidateSourceId">
      <input type="hidden" id="contactCandidateOriginalValue">
      <input type="hidden" id="contactCandidateResolvedFields">
      <input type="checkbox" id="contactCandidateNone">
      <div id="contactCandidateFields">
        <label>Contact name<select id="contactCandidateName" required></select></label>
        <label>Email address<select id="contactCandidateEmail" required></select></label>
        <label>Phone number<select id="contactCandidatePhone"></select></label>
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
