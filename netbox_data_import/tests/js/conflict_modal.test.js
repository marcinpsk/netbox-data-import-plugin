/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Load the first-party browser asset in jsdom and drive the conflict modal the way the
 * preview page does: open it from a row button, then pick one of the offered values. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

const source = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/conflict_modal.js"),
  "utf8",
);

const CONFLICTS = {
  4: {
    device_name: { Name: "L1798 - EH 01", Hostname: "L1798" },
    primary_ip4: { "IP Address (IPv4)": "10.0.0.1", "Management IP Address": "10.0.0.2" },
  },
  5: {
    device_name: { Name: "L1799 - EH 01", Hostname: "L1799" },
  },
};

const LABELS = { device_name: "Device Name", primary_ip4: "Primary IPv4" };

let submitted;

function render() {
  submitted = 0;
  window.ndiPostPreviewAction = vi.fn(() =>
    Promise.resolve({ ok: true, preview_state: "recalculation_required", message: "Saved." }),
  );
  window.ndiMarkPreviewStale = vi.fn();
  document.body.innerHTML = `
    <button id="trigger" data-ndi-modal="#conflictModal" data-row-number="4" data-source-id="L1798">2 conflicts</button>
    <button id="other-trigger" data-ndi-modal="#conflictModal" data-row-number="5" data-source-id="L1799">1 conflict</button>
    <div class="modal" id="conflictModal">
      <form id="conflictForm">
        <input type="hidden" id="conf_source_id" name="source_id">
        <input type="hidden" id="conf_source_column" name="source_column">
        <input type="hidden" id="conf_original_value" name="original_value">
        <input type="hidden" id="conf_resolved_fields" name="resolved_fields">
        <div id="conflictModalBody"></div>
      </form>
    </div>
    <script type="application/json" id="ndi-conflicts-by-row">${JSON.stringify(CONFLICTS)}</script>
    <script type="application/json" id="ndi-target-field-labels">${JSON.stringify(LABELS)}</script>
  `;
  document.getElementById("conflictForm").submit = () => {
    submitted += 1;
  };
  window.eval(source);
  document
    .getElementById("conflictModal")
    .dispatchEvent(
      Object.assign(new Event("show.bs.modal"), { relatedTarget: document.getElementById("trigger") }),
    );
}

function buttons() {
  return [...document.querySelectorAll(".ndi-conflict-resolve-btn")];
}

describe("conflict modal", () => {
  beforeEach(render);

  it("names each field the way the catalog does, not by its key", () => {
    const headings = [...document.querySelectorAll("#conflictModalBody h6")].map((h) => h.textContent);
    expect(headings).toEqual(["Device Name", "Primary IPv4"]);
  });

  it("offers every source column that supplies the field", () => {
    expect(buttons()).toHaveLength(4);
  });

  it("saves the value without navigating away from the preview", async () => {
    buttons()[1].click();
    await vi.waitFor(() => expect(window.ndiPostPreviewAction).toHaveBeenCalledOnce());
    expect(submitted).toBe(0);
    expect(document.getElementById("conf_source_column").value).toBe("_merge_device_name");
    expect(JSON.parse(document.getElementById("conf_resolved_fields").value)).toEqual({
      device_name: "L1798",
    });
    expect(window.ndiMarkPreviewStale).toHaveBeenCalledOnce();
  });

  it("reports that the choice is being saved", () => {
    const picked = buttons()[1];

    picked.click();

    expect(picked.disabled).toBe(true);
    expect(picked.textContent).toMatch(/saving/i);
  });

  it("stops every other choice once one is taken", () => {
    buttons()[1].click();

    expect(buttons().every((b) => b.disabled)).toBe(true);
  });

  it("posts once however many times the operator clicks", async () => {
    const picked = buttons()[1];

    picked.click();
    picked.click();
    buttons()[0].click();

    await vi.waitFor(() => expect(window.ndiPostPreviewAction).toHaveBeenCalledOnce());
    expect(submitted).toBe(0);
  });

  it("does not apply an earlier success to a newly opened conflict", async () => {
    let resolveRequest;
    window.ndiPostPreviewAction.mockImplementationOnce(
      () =>
        new Promise((resolvePending) => {
          resolveRequest = resolvePending;
        }),
    );

    buttons()[1].click();
    document
      .getElementById("conflictModal")
      .dispatchEvent(
        Object.assign(new Event("show.bs.modal"), {
          relatedTarget: document.getElementById("other-trigger"),
        }),
      );
    const currentButtons = buttons();

    resolveRequest({ message: "Saved." });
    await vi.waitFor(() => expect(window.ndiMarkPreviewStale).toHaveBeenCalledOnce());

    expect(currentButtons.every((button) => !button.disabled)).toBe(true);
    expect(currentButtons.every((button) => button.textContent === "Use this")).toBe(true);
  });

  it("does not apply an earlier failure to a newly submitted conflict", async () => {
    const requests = [];
    window.ndiPostPreviewAction.mockImplementation(
      () =>
        new Promise((resolvePending, rejectPending) => {
          requests.push({ resolvePending, rejectPending });
        }),
    );

    buttons()[1].click();
    document
      .getElementById("conflictModal")
      .dispatchEvent(
        Object.assign(new Event("show.bs.modal"), {
          relatedTarget: document.getElementById("other-trigger"),
        }),
      );
    const currentButtons = buttons();
    currentButtons[0].click();

    expect(window.ndiPostPreviewAction).toHaveBeenCalledTimes(2);
    requests[0].rejectPending(new Error("Earlier save failed."));
    await Promise.resolve();

    expect(currentButtons.every((button) => button.disabled)).toBe(true);
    expect(currentButtons[0].textContent).toMatch(/saving/i);

    requests[1].resolvePending({ message: "Saved." });
    await vi.waitFor(() => expect(window.ndiMarkPreviewStale).toHaveBeenCalledOnce());
  });
});
