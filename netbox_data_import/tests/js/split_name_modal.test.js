/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Load the first-party browser asset in jsdom and drive the split modal the way the preview
 * page does: open it from a row button, then send each part of the cell to a Target Field. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const source = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/split_name_modal.js"),
  "utf8",
);

const ORIGINAL_VALUE = "AT900 - host-900";
const SPLIT_FIELD_VALUES = { "cn-2": { device_name: ORIGINAL_VALUE, asset_tag: "", serial: "SN900" } };

let lookups;

function render({ existingResolutions = {} } = {}) {
  lookups = [];
  window.EXISTING_RESOLUTIONS = existingResolutions;
  document.body.innerHTML = `
    <button id="trigger" data-ndi-modal="#splitNameModal" data-source-id="cn-2"
            data-source-column="device_name" data-original-value="${ORIGINAL_VALUE}">Split</button>
    <div class="modal" id="splitNameModal">
      <form id="splitForm" data-check-device-url="/plugins/data-import/check-device/">
        <input type="hidden" id="res_source_id" name="source_id">
        <input type="hidden" id="res_source_column" name="source_column">
        <input type="hidden" id="res_original_value" name="original_value">
        <input type="hidden" id="res_resolved_fields" name="resolved_fields">
        <div id="res_original_display"></div>
        <input type="text" id="res_delimiter" value=" - ">
        <div id="res_existing_notice" class="d-none"><code id="res_existing_display"></code></div>
        <div class="row g-3" id="res_parts_row"></div>
        <div id="res_conflict_alert" class="d-none"></div>
        <div id="res_duplicate_alert" class="d-none"></div>
        <div id="res_device_check" class="d-none"><small id="res_device_check_msg"></small></div>
        <div id="res_save_error" class="d-none" role="alert" aria-live="polite"></div>
        <button type="submit">Save</button>
      </form>
    </div>
    <script type="application/json" id="ndi-split-field-values">${JSON.stringify(SPLIT_FIELD_VALUES)}</script>
  `;
  /* The script binds once and looks every element up by id, so one evaluation serves every
   * render in this file. */
  window.eval(source);
  openModal();
}

function openModal() {
  document
    .getElementById("splitNameModal")
    .dispatchEvent(
      Object.assign(new Event("show.bs.modal", { bubbles: true }), {
        relatedTarget: document.getElementById("trigger"),
      }),
    );
}

function partField(idx) {
  return document.getElementById(`res_part_field_${idx}`);
}

function partValue(idx) {
  return document.getElementById(`res_part_val_${idx}`);
}

function setField(idx, value) {
  partField(idx).value = value;
  partField(idx).dispatchEvent(new Event("change"));
}

function setValue(idx, value) {
  partValue(idx).value = value;
  partValue(idx).dispatchEvent(new Event("input"));
}

function saveButton() {
  return document.querySelector('#splitNameModal button[type="submit"]');
}

function submitForm() {
  return document
    .getElementById("splitForm")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}

function resolvedFields() {
  return JSON.parse(document.getElementById("res_resolved_fields").value || "null");
}

function deferDeviceLookups() {
  const pending = [];
  vi.mocked(fetch).mockImplementation(
    (url) =>
      new Promise((resolveLookup) => {
        lookups.push(url);
        pending.push({ url, resolveLookup });
      }),
  );
  return pending;
}

function respondTo(lookup, data) {
  lookup.resolveLookup({ json: () => Promise.resolve(data) });
}

async function settleDeviceCheck() {
  await new Promise((resolveCheck) => setTimeout(resolveCheck, 0));
}

beforeEach(() => {
  window.ndiPostPreviewAction = vi.fn(() =>
    Promise.resolve({ ok: true, preview_state: "recalculation_required", message: "Saved." }),
  );
  window.ndiMarkPreviewStale = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn((url) => {
      lookups.push(url);
      return Promise.resolve({ json: () => Promise.resolve({ exists: false, count: 0, url: "" }) });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("split modal parts", () => {
  beforeEach(() => render());

  it("cuts the cell on the delimiter, one part per field", () => {
    expect([partValue(0).value, partValue(1).value]).toEqual(["AT900", "host-900"]);
    expect([partField(0).value, partField(1).value]).toEqual(["asset_tag", "device_name"]);
  });

  it("re-cuts the cell when the delimiter changes", () => {
    const delimiter = document.getElementById("res_delimiter");
    delimiter.value = "-";
    delimiter.dispatchEvent(new Event("input", { bubbles: true }));
    expect([partValue(0).value, partValue(1).value, partValue(2).value]).toEqual(["AT900", "host", "900"]);
  });

  it("saves the field each part was sent to without navigating away", async () => {
    submitForm();
    expect(resolvedFields()).toEqual({ asset_tag: "AT900", device_name: "host-900" });
    await vi.waitFor(() => expect(window.ndiPostPreviewAction).toHaveBeenCalledOnce());
    expect(window.ndiMarkPreviewStale).toHaveBeenCalledOnce();
  });

  it("keeps a successful save successful without optional response details", async () => {
    window.ndiPostPreviewAction = vi.fn().mockResolvedValueOnce({ ok: true });
    window.ndiMarkPreviewStale = undefined;

    submitForm();

    await vi.waitFor(() => expect(saveButton().textContent).toBe("Saved"));
    expect(saveButton().title).toBe("Resolution saved.");
    expect(document.getElementById("res_save_error").classList.contains("d-none")).toBe(true);
  });

  it("leaves native submission available when the preview-action helper is unavailable", () => {
    window.ndiPostPreviewAction = undefined;

    expect(submitForm()).toBe(true);

    expect(saveButton().disabled).toBe(false);
    expect(saveButton().textContent).toBe("Save resolution");
  });

  it("does not offer the source identity as a resolution target", () => {
    expect([...partField(0).options].map((option) => option.value)).not.toContain("source_id");
  });

  it("restores the unsaved button state when the modal opens again", async () => {
    submitForm();
    await vi.waitFor(() => expect(saveButton().textContent).toBe("Saved"));
    expect(saveButton().title).toBe("Saved.");

    openModal();

    expect(saveButton().textContent).toBe("Save resolution");
    expect(saveButton().title).toBe("");
  });

  it("reports a save failure inside the modal and clears it when reopened", async () => {
    window.ndiPostPreviewAction = vi
      .fn()
      .mockRejectedValueOnce(new Error("Resolution was rejected."))
      .mockResolvedValueOnce({ ok: true, preview_state: "recalculation_required", message: "Saved." });

    submitForm();

    const alertBox = document.getElementById("res_save_error");
    await vi.waitFor(() => expect(alertBox.textContent).toBe("Resolution was rejected."));
    expect(alertBox.classList.contains("d-none")).toBe(false);
    expect(saveButton().title).toBe("");

    submitForm();

    expect(alertBox.textContent).toBe("");
    expect(alertBox.classList.contains("d-none")).toBe(true);
    await vi.waitFor(() => expect(saveButton().textContent).toBe("Saved"));

    openModal();

    expect(alertBox.textContent).toBe("");
    expect(alertBox.classList.contains("d-none")).toBe(true);
  });

  it("explains a save rejection that has no message", async () => {
    window.ndiPostPreviewAction = vi.fn().mockRejectedValueOnce({});

    submitForm();

    const alertBox = document.getElementById("res_save_error");
    await vi.waitFor(() => expect(alertBox.textContent).toBe("Could not save the resolution."));
    expect(alertBox.classList.contains("d-none")).toBe(false);
  });
});

describe("the device that a part would name", () => {
  beforeEach(() => render());

  it("looks up the part the operator sent to the device name, not the second one", () => {
    lookups.length = 0;
    setField(0, "device_name");
    setField(1, "asset_tag");
    expect(lookups.at(-1)).toContain("AT900");
    expect(lookups.filter((url) => url.includes("host-900"))).toHaveLength(0);
  });

  it("looks up the second part while that is the one naming the device", () => {
    expect(lookups.some((url) => url.includes("host-900"))).toBe(true);
  });

  it("follows the value as it is edited", () => {
    lookups.length = 0;
    setValue(1, "host-901");
    expect(lookups.some((url) => url.includes("host-901"))).toBe(true);
  });
});

describe("device checks that finish out of order", () => {
  it("keeps the newer result when the older lookup finishes last", async () => {
    const pending = deferDeviceLookups();
    render();
    setField(1, "asset_tag");
    setField(0, "device_name");

    expect(pending.map(({ url }) => url)).toEqual([
      "/plugins/data-import/check-device/?name=host-900",
      "/plugins/data-import/check-device/?name=AT900",
    ]);
    respondTo(pending[1], { exists: false, count: 0, url: "" });
    await vi.waitFor(() => expect(document.getElementById("res_device_check_msg").textContent).toContain("AT900"));

    respondTo(pending[0], { exists: true, count: 1, url: "/dcim/devices/900/" });
    await settleDeviceCheck();

    const message = document.getElementById("res_device_check_msg").textContent;
    expect(message).toContain("AT900");
    expect(message).toContain("not yet in NetBox");
  });

  it("waits for the newer result when the older lookup finishes first", async () => {
    const pending = deferDeviceLookups();
    render();
    setField(1, "asset_tag");
    setField(0, "device_name");

    respondTo(pending[0], { exists: true, count: 1, url: "/dcim/devices/900/" });
    await settleDeviceCheck();
    expect(document.getElementById("res_device_check").classList.contains("d-none")).toBe(true);

    respondTo(pending[1], { exists: false, count: 0, url: "" });
    await vi.waitFor(() => expect(document.getElementById("res_device_check_msg").textContent).toContain("AT900"));
    expect(document.getElementById("res_device_check_msg").textContent).toContain("not yet in NetBox");
  });

  it("stays hidden when no part names the device", async () => {
    const pending = deferDeviceLookups();
    render();
    setField(1, "serial");

    respondTo(pending[0], { exists: true, count: 1, url: "/dcim/devices/900/" });
    await settleDeviceCheck();

    expect(document.getElementById("res_device_check").classList.contains("d-none")).toBe(true);
  });
});

describe("two parts claiming one field", () => {
  beforeEach(() => render());

  it("refuses the save instead of dropping a part", () => {
    setField(0, "device_name");
    expect(saveButton().disabled).toBe(true);
    expect(document.getElementById("res_duplicate_alert").classList.contains("d-none")).toBe(false);
  });

  it("marks both offending selects", () => {
    setField(0, "device_name");
    expect(partField(0).classList.contains("is-invalid")).toBe(true);
    expect(partField(1).classList.contains("is-invalid")).toBe(true);
  });

  it("writes no resolution while the clash stands", () => {
    setField(0, "device_name");
    submitForm();
    expect(resolvedFields()).toBeNull();
  });

  it("clears the refusal once one part moves to another field", () => {
    setField(0, "device_name");
    setField(0, "asset_tag");
    expect(saveButton().disabled).toBe(false);
    expect(document.getElementById("res_duplicate_alert").classList.contains("d-none")).toBe(true);
    expect(partField(1).classList.contains("is-invalid")).toBe(false);
  });

  it("leaves two ignored parts alone", () => {
    setField(0, "");
    setField(1, "");
    expect(saveButton().disabled).toBe(false);
  });
});

describe("a part that overwrites a value the file already carries", () => {
  beforeEach(() => render());

  it("blocks the save until the override is acknowledged", () => {
    setField(0, "serial");
    expect(saveButton().disabled).toBe(true);
    document.getElementById("res_force_0").checked = true;
    document.getElementById("res_force_0").dispatchEvent(new Event("change"));
    expect(saveButton().disabled).toBe(false);
  });
});

describe("a row that already has a saved resolution", () => {
  it("pre-fills the parts from the resolution", () => {
    render({
      existingResolutions: {
        "cn-2": {
          device_name: { original_value: ORIGINAL_VALUE, resolved_fields: { serial: "SN900", device_name: "host-900" } },
        },
      },
    });
    expect([partField(0).value, partField(1).value]).toEqual(["serial", "device_name"]);
    expect([partValue(0).value, partValue(1).value]).toEqual(["SN900", "host-900"]);
    expect(document.getElementById("res_existing_notice").classList.contains("d-none")).toBe(false);
  });
});
