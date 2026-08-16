/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Load the first-party browser asset in jsdom and drive it with real selects and
 * Tom Select instances, as the page does. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
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
  "second-row": {
    contact: {
      "Owner Name": "Second Contact",
      "Owner Email": "second@example.invalid",
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

function addPreviewFixture(resolutions = {}) {
  document.body.innerHTML = `
    <div id="contactCandidateModal">
      <form id="contactCandidateForm" data-contact-lookup-field="email" data-contact-lookup-url="/contact-lookup/">
        <input type="hidden" id="contactCandidateSourceId">
        <input type="hidden" id="contactCandidateOriginalValue">
        <input type="hidden" id="contactCandidateResolvedFields">
        <input type="hidden" id="contactCandidateContactId">
        <input type="checkbox" id="contactCandidateNone">
        <div id="contactCandidateFields">
          <div id="contactCandidateSuggestion" class="d-none"></div>
          <select id="contactCandidateExisting"></select>
          <select id="contactCandidateName"></select>
          <input id="contactCandidateNameValue">
          <select id="contactCandidateEmail"></select>
          <input id="contactCandidateEmailValue">
          <select id="contactCandidatePhone"></select>
          <input id="contactCandidatePhoneValue">
        </div>
      </form>
    </div>
    <script id="ndi-candidate-values-by-row" type="application/json">${JSON.stringify(candidates)}</script>
    <script id="ndi-contact-suggestions-by-row" type="application/json">${JSON.stringify(contactSuggestions)}</script>
  `;
  window.EXISTING_RESOLUTIONS = resolutions;
  window.TomSelect = TomSelect;
  for (const id of ["contactCandidateName", "contactCandidateEmail", "contactCandidatePhone"]) {
    new TomSelect(document.getElementById(id), { create: false });
  }
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

function widgetOptionTexts(select) {
  select.nextElementSibling.querySelector(".ts-control").dispatchEvent(new MouseEvent("click", { bubbles: true }));
  select.tomselect.refreshOptions(true);
  return [...select.nextElementSibling.querySelectorAll(".ts-dropdown .option")].map((option) => option.textContent);
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("contact candidate modal", () => {
  it("keeps candidate options visible in Tom Select after opening a row", () => {
    addPreviewFixture();
    openRow("first-row", "source-first");

    const select = document.getElementById("contactCandidateName");
    expect([...select.options].map((option) => option.value)).toEqual(["", "Contact Name", "Contact Email"]);
    expect(widgetOptionTexts(select)).toEqual(["Contact Name: First Contact", "Contact Email: first@example.invalid"]);
  });

  it("shows saved sources and replaces widget options when another row opens", () => {
    addPreviewFixture({
      "source-first": {
        "candidate:contact": {
          resolved_fields: {
            contact_resolution_applied: true,
            contact_field_sources: { name: "Contact Name", email: "Contact Email" },
          },
        },
      },
    });
    openRow("first-row", "source-first");

    const name = document.getElementById("contactCandidateName");
    const email = document.getElementById("contactCandidateEmail");
    expect(name.value).toBe("Contact Name");
    expect(name.tomselect.getValue()).toBe("Contact Name");
    expect(email.value).toBe("Contact Email");
    expect(email.tomselect.getValue()).toBe("Contact Email");

    openRow("second-row", "source-second");
    expect(widgetOptionTexts(name)).toEqual(["Owner Name: Second Contact", "Owner Email: second@example.invalid"]);
    expect(name.tomselect.getValue()).toBe("");
  });

  it("disables and re-enables both native and Tom Select controls for no contact", () => {
    addPreviewFixture();
    openRow("first-row", "source-first");

    const checkbox = document.getElementById("contactCandidateNone");
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event("change", { bubbles: true }));
    for (const id of ["contactCandidateName", "contactCandidateEmail", "contactCandidatePhone"]) {
      const select = document.getElementById(id);
      expect(select.disabled).toBe(true);
      expect(select.tomselect.isDisabled).toBe(true);
    }

    checkbox.checked = false;
    checkbox.dispatchEvent(new Event("change", { bubbles: true }));
    for (const id of ["contactCandidateName", "contactCandidateEmail", "contactCandidatePhone"]) {
      const select = document.getElementById(id);
      expect(select.disabled).toBe(false);
      expect(select.tomselect.isDisabled).toBe(false);
    }
  });

  it("saves typed Contact details when no source column supplies them", () => {
    addPreviewFixture();
    openRow("second-row", "source-second");
    document.getElementById("contactCandidateNameValue").value = "Typed Contact";
    document.getElementById("contactCandidateEmailValue").value = "typed@example.invalid";
    document.getElementById("contactCandidatePhoneValue").value = "+1 202-555-0104";

    document.getElementById("contactCandidateForm").dispatchEvent(new Event("submit", { cancelable: true }));

    expect(JSON.parse(document.getElementById("contactCandidateResolvedFields").value)).toEqual({
      contact_resolution_applied: true,
      contact_field_sources: {},
      contact_field_values: {
        name: "Typed Contact",
        email: "typed@example.invalid",
        phone: "+1 202-555-0104",
      },
      contact_id: null,
    });
  });

  it("offers the matched NetBox Contact and copies its current details", () => {
    addPreviewFixture();
    openRow("first-row", "source-first");
    const existing = document.getElementById("contactCandidateExisting");

    expect(existing.tomselect.options["41"].email).toBe("first@example.invalid");
    expect(document.getElementById("contactCandidateSuggestion").classList.contains("d-none")).toBe(false);
    existing.tomselect.setValue("41");

    expect(document.getElementById("contactCandidateContactId").value).toBe("41");
    expect(document.getElementById("contactCandidateNameValue").value).toBe("Existing First Contact");
    expect(document.getElementById("contactCandidateEmailValue").value).toBe("first@example.invalid");
    expect(document.getElementById("contactCandidatePhoneValue").value).toBe("+1 202-555-0103");
  });

  it("submits no Contact after an existing Contact was selected", () => {
    addPreviewFixture();
    openRow("first-row", "source-first");
    document.getElementById("contactCandidateExisting").tomselect.setValue("41");
    const checkbox = document.getElementById("contactCandidateNone");
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event("change", { bubbles: true }));

    document.getElementById("contactCandidateForm").dispatchEvent(new Event("submit", { cancelable: true }));

    expect(JSON.parse(document.getElementById("contactCandidateResolvedFields").value)).toEqual({
      contact_resolution_applied: true,
      contact_field_sources: {},
      contact_field_values: {},
      contact_id: null,
    });
  });
});
