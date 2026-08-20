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
const rowActionsSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/preview_row_actions.js"),
  "utf8",
);

const previewFixture = `
  <base href="http://preview.test/">
  <input type="hidden" name="csrfmiddlewaretoken" value="csrf-token">
  <input type="hidden" id="ndi-preview-revision" value="revision-one">
  <div id="ndi-preview-stale" hidden>Saved changes are pending.</div>
  <button type="button" id="ndi-run-import">Run Import</button>
  <button type="button" class="btn btn-outline-warning" data-ndi-modal="#contactCandidateModal"
          data-source-id="source-first" data-row-number="first-row">Resolve contact fields</button>
  <div id="contactCandidateModal">
    <form id="contactCandidateForm" action="/save-resolution/"
          data-contact-lookup-field="email" data-contact-lookup-url="/contact-lookup/">
      <div class="modal-body"></div>
      <input type="hidden" name="profile_id" value="7">
      <input type="hidden" name="source_column" value="candidate:contact">
      <input type="hidden" name="source_id" id="contactCandidateSourceId">
      <input type="hidden" name="original_value" id="contactCandidateOriginalValue">
      <input type="hidden" name="resolved_fields" id="contactCandidateResolvedFields">
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
      <button type="submit">Save &amp; re-run preview</button>
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
      },
      "source-literal": {
        "candidate:contact": {
          "resolved_fields": {
            "contact_resolution_applied": true,
            "contact_field_sources": {},
            "contact_field_values": { "name": "Typed Name" }
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

/** Install a stub for the shared row-action helper and record what it was called with. */
async function stubRowAction(page, { fails = null } = {}) {
  await page.evaluate((failure) => {
    window.__calls = [];
    window.__staleMarked = 0;
    window.ndiPostPreviewAction = (url, body) => {
      window.__calls.push({ url, fields: Object.fromEntries(body.entries()) });
      return failure ? Promise.reject(new Error(failure)) : Promise.resolve({ ok: true });
    };
    window.ndiMarkPreviewStale = () => { window.__staleMarked += 1; };
  }, fails);
}

test("saving posts through the row-action helper instead of navigating", async ({ page }) => {
  await setUp(page);
  await stubRowAction(page);
  await openRow(page, "first-row", "source-first");

  await page.evaluate(() => document.getElementById("contactCandidateForm")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

  const calls = await page.evaluate(() => window.__calls);
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toContain("/save-resolution/");
  expect(JSON.parse(calls[0].fields.resolved_fields).contact_field_sources).toEqual({
    email: "Primary Contact",
    name: "Contact",
    phone: "Contact Number",
  });
  expect(await page.evaluate(() => window.__staleMarked)).toBe(1);
});

test("a saved row reports itself resolved without a recalculation", async ({ page }) => {
  await setUp(page);
  await stubRowAction(page);
  await openRow(page, "first-row", "source-first");

  await page.evaluate(() => document.getElementById("contactCandidateForm")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

  const button = page.locator('[data-ndi-modal="#contactCandidateModal"][data-source-id="source-first"]');
  await expect(button).toHaveClass(/ndi-contact-resolved/);
  await expect(button).not.toHaveClass(/btn-outline-warning/);
  await expect(button).toContainText("Contact resolved");
});

test("a refused save states the reason in the modal and keeps it open", async ({ page }) => {
  await setUp(page);
  await stubRowAction(page, { fails: "The preview was recalculated in another tab." });
  await openRow(page, "first-row", "source-first");

  await page.evaluate(() => document.getElementById("contactCandidateForm")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

  await expect(page.locator("#contactCandidateError")).toHaveText(
    "The preview was recalculated in another tab.",
  );
  // The row is not resolved, so its button must still ask for a decision.
  await expect(
    page.locator('[data-ndi-modal="#contactCandidateModal"][data-source-id="source-first"]'),
  ).not.toHaveClass(/ndi-contact-resolved/);
  expect(await page.evaluate(() => window.__staleMarked)).toBe(0);
});

test("a saved decision is what the row shows when it is reopened", async ({ page }) => {
  await setUp(page);
  await stubRowAction(page);
  await openRow(page, "first-row", "source-first");

  // Decline a contact for this row, which is the furthest a decision can sit from the proposal.
  await page.locator("#contactCandidateNone").check();
  await page.evaluate(() => document.getElementById("contactCandidateForm")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

  await openRow(page, "first-row", "source-first");

  await expect(page.locator("#contactCandidateNone")).toBeChecked();
  expect(Object.values(await rolesByColumn(page)).filter(Boolean)).toEqual([]);
  // Re-saving must not resurrect the proposal over the stored decision.
  const payload = await page.evaluate(() => {
    window.__calls.length = 0;
    document.getElementById("contactCandidateForm")
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    return null;
  });
  void payload;
  const calls = await page.evaluate(() => window.__calls);
  expect(JSON.parse(calls[0].fields.resolved_fields).contact_field_sources).toEqual({});
});

test("a failure on one row is not shown when another row opens", async ({ page }) => {
  await setUp(page);
  await stubRowAction(page, { fails: "boom for the first row" });
  await openRow(page, "first-row", "source-first");
  await page.evaluate(() => document.getElementById("contactCandidateForm")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await expect(page.locator("#contactCandidateError")).toHaveText("boom for the first row");

  await openRow(page, "saved-row", "source-saved");

  await expect(page.locator("#contactCandidateError")).toHaveCount(0);
});

/* Hold the save open so the modal can be driven while a request is still in flight. */
async function setUpWithPendingSave(page) {
  await page.setContent(previewFixture);
  await initNetBoxSelects(page);
  await page.addScriptTag({ content: rowActionsSource });
  await page.addScriptTag({ content: controllerSource });
  await page.evaluate(() => {
    window.__requests = [];
    const realFetch = window.fetch.bind(window);
    window.__release = [];
    window.__hides = 0;
    window.Modal = { getOrCreateInstance: () => ({ hide: () => { window.__hides += 1; } }) };
    window.fetch = (url, options) => {
      if (!String(url).includes("/save-resolution/")) return realFetch(url, options);
      window.__requests.push({ url, fields: Object.fromEntries(options.body.entries()) });
      return new Promise((resolve, reject) => {
        window.__release.push({
          ok: () => resolve({ ok: true, status: 200, json: () => Promise.resolve(
            { ok: true, row_number: 1, preview_state: "recalculation_required", message: "Saved." }) }),
          fail: () => reject(new Error("save A failed")),
        });
      });
    };
  });
}

/* The two controllers together, with only the network stubbed. This is the pair that ships:
 * a stubbed helper would keep passing if `preview_row_actions.js` stopped exporting it. */
async function setUpBothControllers(page, { status = 200, payload = null } = {}) {
  await page.setContent(previewFixture);
  await initNetBoxSelects(page);
  await page.addScriptTag({ content: rowActionsSource });
  await page.addScriptTag({ content: controllerSource });
  await page.evaluate(
    ({ status: code, payload: responseBody }) => {
      window.__requests = [];
      const realFetch = window.fetch.bind(window);
      window.fetch = (url, options) => {
        // Only the save is stubbed; the Contact lookup still goes out to its route.
        if (!String(url).includes("/save-resolution/")) return realFetch(url, options);
        window.__requests.push({
          url,
          revision: options.body.get("preview_revision"),
          csrf: options.headers["X-CSRFToken"],
          accept: options.headers.Accept,
          fields: Object.fromEntries(options.body.entries()),
        });
        return Promise.resolve({
          ok: code < 400,
          status: code,
          json: () => Promise.resolve(
            responseBody || { ok: true, row_number: 1, preview_state: "recalculation_required", message: "Saved." },
          ),
        });
      };
    },
    { status, payload },
  );
}

test("the modal saves through the shipped row-action helper, not a copy of it", async ({ page }) => {
  await setUpBothControllers(page);
  await openRow(page, "first-row", "source-first");

  await page.evaluate(() => document.getElementById("contactCandidateForm")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await expect(page.locator("#ndi-preview-stale")).toBeVisible();

  const requests = await page.evaluate(() => window.__requests);
  expect(requests).toHaveLength(1);
  expect(requests[0].url).toContain("/save-resolution/");
  // The helper owns the revision stamp and the JSON negotiation, so both must arrive.
  expect(requests[0].revision).toBe("revision-one");
  expect(requests[0].csrf).toBe("csrf-token");
  expect(requests[0].accept).toBe("application/json");
  await expect(page.locator("#ndi-run-import")).toBeDisabled();
});

test("a server envelope without the recalculation state is refused", async ({ page }) => {
  await setUpBothControllers(page, { payload: { ok: true, message: "Saved." } });
  await openRow(page, "first-row", "source-first");

  await page.evaluate(() => document.getElementById("contactCandidateForm")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

  await expect(page.locator("#contactCandidateError")).toContainText("invalid state");
  await expect(page.locator("#ndi-preview-stale")).toBeHidden();
});

test("the save button is usable again on the next row", async ({ page }) => {
  await setUpBothControllers(page);
  await openRow(page, "first-row", "source-first");
  // Press the real control, so a disabled button would stop the test the way it stops an operator.
  await page.locator("#contactCandidateForm button[type=submit]").click();
  await expect(page.locator("#ndi-preview-stale")).toBeVisible();

  // `saved-row` stores a name only, so opening it proves the button is restored for a row the
  // required-field check would still stop.
  await openRow(page, "saved-row", "source-saved");
  await expect(page.locator("#contactCandidateForm button[type=submit]")).toBeEnabled();

  await openRow(page, "first-row", "source-first");

  const save = page.locator("#contactCandidateForm button[type=submit]");
  await expect(save).toBeEnabled();
  await expect(save).toHaveText(/Save/);
  await save.click();
  expect(await page.evaluate(() => window.__requests.length)).toBe(2);
});

test("filling a missing field from a candidate row clears the block on saving", async ({ page }) => {
  await setUpBothControllers(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateEditToggle").click();

  // Strip the name, submit to raise the validity message, then supply a name from a row.
  await page.locator('.ndi-contact-value-row[data-source-column="Contact"] .ndi-contact-role')
    .selectOption("");
  await page.locator("#contactCandidateForm button[type=submit]").click();
  await expect(page.locator(".ndi-contact-literal")).toHaveCount(1);
  await page.locator('.ndi-contact-value-row[data-source-column="Owner"] .ndi-contact-role')
    .selectOption("name");

  await page.locator("#contactCandidateForm button[type=submit]").click();

  const requests = await page.evaluate(() => window.__requests);
  expect(requests).toHaveLength(1);
  expect(JSON.parse(requests[0].fields.resolved_fields).contact_field_sources.name).toBe("Owner");
});

test("a linked Contact is still shown when the saved row is reopened", async ({ page }) => {
  await page.route("**/contact-lookup/?q=*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        results: [{ id: 91, name: "Late Contact", email: "late@example.invalid", phone: "+1 202-555-0199" }],
      }),
    });
  });
  await setUpBothControllers(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateLinkExisting").click();
  await page.locator("#contactCandidateExisting + .ts-wrapper .ts-control input").fill("late");
  await page.locator("#contactCandidateExisting + .ts-wrapper .ts-dropdown .option").click();
  await page.locator("#contactCandidateForm button[type=submit]").click();
  await expect(page.locator("#ndi-preview-stale")).toBeVisible();

  await openRow(page, "first-row", "source-first");

  await expect(page.locator("#contactCandidateContactId")).toHaveValue("91");
  await expect(page.locator("#contactCandidateSummaryName")).toHaveText("Late Contact");
});

test("the missing-field message lands on an empty input, not a filled one", async ({ page }) => {
  await setUpBothControllers(page);
  // This row stores a typed name and no email, so a saved literal is already on screen.
  await openRow(page, "saved-row", "source-literal");
  await page.locator("#contactCandidateEditToggle").click();
  await expect(page.locator(".ndi-contact-literal")).toHaveValue("Typed Name");

  await page.locator("#contactCandidateForm button[type=submit]").click();

  const literals = await page.evaluate(() =>
    [...document.querySelectorAll(".ndi-contact-literal")].map((input) => ({
      value: input.value,
      message: input.validationMessage,
    })),
  );
  const named = literals.find((input) => input.value === "Typed Name");
  expect(named.message).toBe("");
  // The email is what is missing, so an empty input has to carry the message.
  expect(literals.some((input) => input.value === "" && /email/i.test(input.message))).toBe(true);
  expect(await page.evaluate(() => window.__requests.length)).toBe(0);
});

test("reopening the modal during a save cannot start a second one", async ({ page }) => {
  await setUpWithPendingSave(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateForm button[type=submit]").click();
  expect(await page.evaluate(() => window.__requests.length)).toBe(1);

  // The operator closes the modal and opens another row while the first save is still open.
  await openRow(page, "first-row", "source-first");

  await expect(page.locator("#contactCandidateForm button[type=submit]")).toBeDisabled();
  expect(await page.evaluate(() => window.__requests.length)).toBe(1);
});

test("linking a Contact clears a validation message left on a literal", async ({ page }) => {
  await page.route("**/contact-lookup/?q=*", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        results: [{ id: 77, name: "Rescue Contact", email: "rescue@example.invalid", phone: "" }],
      }),
    });
  });
  await setUpBothControllers(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateEditToggle").click();
  // Drop the name so the required-field check marks a literal invalid.
  await page.locator('.ndi-contact-value-row[data-source-column="Contact"] .ndi-contact-role')
    .selectOption("");
  await page.locator("#contactCandidateForm button[type=submit]").click();
  expect(await page.evaluate(() => window.__requests.length)).toBe(0);

  await page.locator("#contactCandidateLinkExisting").click();
  await page.locator("#contactCandidateExisting + .ts-wrapper .ts-control input").fill("rescue");
  await page.locator("#contactCandidateExisting + .ts-wrapper .ts-dropdown .option").click();
  await page.locator("#contactCandidateForm button[type=submit]").click();

  // A linked Contact supplies every field, so nothing may still block the save.
  const requests = await page.evaluate(() => window.__requests);
  expect(requests).toHaveLength(1);
  expect(JSON.parse(requests[0].fields.resolved_fields).contact_id).toBe(77);
});

test("a save that settles late leaves the row now on screen alone", async ({ page }) => {
  await setUpWithPendingSave(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateForm button[type=submit]").click();

  // The operator moves on to another row while the first save is still open.
  await openRow(page, "saved-row", "source-saved");
  await page.evaluate(() => window.__release[0].ok());

  // The late response belongs to the row that is gone, so it must not close this one.
  await expect(page.locator("#contactCandidateSourceId")).toHaveValue("source-saved");
  await expect(page.locator("#contactCandidateError")).toHaveCount(0);
  expect(await page.evaluate(() => window.__hides)).toBe(0);
});

test("a late failure does not report itself against another row", async ({ page }) => {
  await setUpWithPendingSave(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateForm button[type=submit]").click();
  await openRow(page, "saved-row", "source-saved");

  await page.evaluate(() => window.__release[0].fail());

  await expect(page.locator("#contactCandidateError")).toHaveCount(0);
});

test("a reused blank input is given the role it was asked for", async ({ page }) => {
  await setUpBothControllers(page);
  await openRow(page, "first-row", "source-first");
  await page.locator("#contactCandidateEditToggle").click();
  // A blank row the operator added themselves, still marked "Not used".
  await page.locator("#contactCandidateAddValue").click();
  await page.locator('.ndi-contact-value-row[data-source-column="Primary Contact"] .ndi-contact-role')
    .selectOption("");
  await page.locator("#contactCandidateForm button[type=submit]").click();

  // The blank input now carries the email message, so typing there must satisfy the check.
  await page.locator(".ndi-contact-literal").fill("typed@example.invalid");
  await page.locator("#contactCandidateForm button[type=submit]").click();

  const requests = await page.evaluate(() => window.__requests);
  expect(requests).toHaveLength(1);
  expect(JSON.parse(requests[0].fields.resolved_fields).contact_field_values.email).toBe(
    "typed@example.invalid",
  );
});
