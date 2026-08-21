/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Load the first-party browser asset in jsdom and drive it with real selects and
 * Tom Select instances, as the page does.
 *
 * The rendered flows live in `tests/browser/contact_candidate_modal.spec.js`, which runs the
 * same controller in a real browser. This file keeps the cases a rendered test cannot reach:
 * a missing lookup URL, the network call behind the picker, and a page with no saved state. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TomSelect from "tom-select";

const controllerPath = resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/contact_candidate_modal.js",
);
const controllerSource = readFileSync(controllerPath, "utf8");

const candidates = {
  "first-row": {
    contact: {
      "Contact Name": "First Contact",
      "Contact Email": "first@example.invalid",
    },
  },
};

const contactSuggestions = {
  "first-row": {
    id: 41,
    name: "Existing First Contact",
    email: "first@example.invalid",
    phone: "+1 202-555-0103",
  },
};

const roleSuggestions = {
  "first-row": { name: "Contact Name", email: "Contact Email" },
};

function addPreviewFixture(resolutions = {}, { lookupUrl = "/contact-lookup/", lookupField = "email" } = {}) {
  const lookupAttribute = lookupUrl === null ? "" : ` data-contact-lookup-url="${lookupUrl}"`;
  document.body.innerHTML = `
    <div id="contactCandidateModal">
      <form id="contactCandidateForm" data-contact-lookup-field="${lookupField}"${lookupAttribute}>
        <input type="hidden" id="contactCandidateSourceId">
        <input type="hidden" id="contactCandidateOriginalValue">
        <input type="hidden" id="contactCandidateResolvedFields">
        <input type="hidden" id="contactCandidateContactId">
        <div id="contactCandidateSummary">
          <div id="contactCandidateSummaryName"></div>
          <div id="contactCandidateSummaryEmail"></div>
          <div id="contactCandidateSummaryPhone"></div>
          <button type="button" id="contactCandidateEditToggle" aria-expanded="false"></button>
        </div>
        <div id="contactCandidateProvenance"></div>
        <div id="contactCandidateSuggestion" class="d-none"></div>
        <div id="contactCandidateEdit" hidden>
          <div id="contactCandidateValueRows"></div>
          <button type="button" id="contactCandidateAddValue"></button>
        </div>
        <button type="button" id="contactCandidateLinkExisting" aria-expanded="false"></button>
        <input type="checkbox" id="contactCandidateNone">
        <div id="contactCandidateExistingWrap" hidden>
          <select id="contactCandidateExisting"></select>
        </div>
      </form>
    </div>
    <script id="ndi-candidate-values-by-row" type="application/json">${JSON.stringify(candidates)}</script>
    <script id="ndi-contact-suggestions-by-row" type="application/json">${JSON.stringify(contactSuggestions)}</script>
    <script id="ndi-contact-role-suggestions-by-row" type="application/json">${JSON.stringify(roleSuggestions)}</script>
  `;
  window.EXISTING_RESOLUTIONS = resolutions;
  /* NetBox enhances every plain <select> itself and never exposes TomSelect on
   * `window`, so the fixture initializes the select the same way its
   * initStaticSelects() does. */
  new TomSelect(document.getElementById("contactCandidateExisting"), { create: false, maxOptions: undefined });
  window.eval(controllerSource);
}

function openRow(rowNumber, sourceId) {
  const button = document.createElement("button");
  button.dataset.rowNumber = rowNumber;
  button.dataset.sourceId = sourceId;
  const event = new Event("show.bs.modal");
  Object.defineProperty(event, "relatedTarget", { value: button });
  document.getElementById("contactCandidateModal").dispatchEvent(event);
}

function rolesByColumn() {
  return Object.fromEntries(
    [...document.querySelectorAll("#contactCandidateValueRows .ndi-contact-value-row")].map((row) => [
      row.dataset.sourceColumn,
      row.querySelector(".ndi-contact-role").value,
    ]),
  );
}

function submitPayload() {
  const form = document.getElementById("contactCandidateForm");
  const blocker = (event) => event.preventDefault();
  form.addEventListener("submit", blocker);
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  form.removeEventListener("submit", blocker);
  return JSON.parse(document.getElementById("contactCandidateResolvedFields").value || "{}");
}

beforeEach(() => {
  document.body.innerHTML = "";
});

// A stub restored at the end of a test body survives a failed assertion above it and turns
// one failure into several.
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("contact candidate modal", () => {
  it("opens a row when no saved-resolution global is present", () => {
    addPreviewFixture();
    delete window.EXISTING_RESOLUTIONS;

    expect(() => openRow("first-row", "source-first")).not.toThrow();
    expect(rolesByColumn()).toEqual({ "Contact Name": "name", "Contact Email": "email" });
  });

  it("keeps the contact picker usable when the template omits the lookup URL", async () => {
    addPreviewFixture({}, { lookupUrl: null });
    openRow("first-row", "source-first");

    const picker = document.getElementById("contactCandidateExisting").tomselect;
    const results = await new Promise((done) => picker.settings.load("ada", done));

    expect(results).toBeUndefined();
  });

  it("finds Contacts created after the page loaded through the lookup endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      json: () =>
        Promise.resolve({
          results: [{ id: 92, name: "Fresh Contact", email: "fresh@example.invalid", phone: "" }],
        }),
    });
    vi.stubGlobal("fetch", fetchMock);
    addPreviewFixture();
    openRow("first-row", "source-first");

    const picker = document.getElementById("contactCandidateExisting").tomselect;
    const results = await new Promise((done) => picker.settings.load("fresh", done));

    expect(fetchMock).toHaveBeenCalledWith(
      "/contact-lookup/?q=fresh",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(results[0].name).toBe("Fresh Contact");
  });

  it("asks for a query of at least two characters before calling the endpoint", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    addPreviewFixture();
    openRow("first-row", "source-first");

    const picker = document.getElementById("contactCandidateExisting").tomselect;
    await new Promise((done) => picker.settings.load("a", done));

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("submits when the profile stores no contact lookup field", () => {
    /* AdapterSettings returns an explicitly stored empty string, and a blank role can never be
     * satisfied, so requiring it would block every save on such a profile. */
    addPreviewFixture({}, { lookupField: "" });
    openRow("first-row", "source-first");

    expect(submitPayload().contact_resolution_applied).toBe(true);
  });

  it("reopens a saved literal value as its own row", () => {
    addPreviewFixture({
      "source-first": {
        "candidate:contact": {
          resolved_fields: {
            contact_resolution_applied: true,
            contact_field_sources: {},
            contact_field_values: { name: "Typed Name", email: "typed@example.invalid" },
          },
        },
      },
    });
    openRow("first-row", "source-first");

    const literals = [...document.querySelectorAll(".ndi-contact-value-row[data-literal]")];
    expect(literals.map((row) => row.querySelector(".ndi-contact-literal").value)).toEqual([
      "Typed Name",
      "typed@example.invalid",
    ]);
    expect(submitPayload().contact_field_values).toEqual({
      name: "Typed Name",
      email: "typed@example.invalid",
    });
  });

  it("keeps the row's own values when a matching NetBox Contact is only offered", () => {
    addPreviewFixture();
    openRow("first-row", "source-first");

    // The suggestion opens the picker, but nothing is linked until the operator chooses.
    expect(document.getElementById("contactCandidateSuggestion").classList.contains("d-none")).toBe(false);
    expect(document.getElementById("contactCandidateExistingWrap").hidden).toBe(false);
    expect(document.getElementById("contactCandidateContactId").value).toBe("");
    expect(submitPayload().contact_field_sources).toEqual({
      name: "Contact Name",
      email: "Contact Email",
    });
  });
});
