/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Load the first-party browser asset in jsdom and drive its real delegated click
 * handler against the row markup the preview page renders. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import Modal from "bootstrap/js/dist/modal.js";
import TomSelect from "tom-select";

const controllerSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/preview_row_controls.js"),
  "utf8",
);

let controllerLoaded = false;

/* The head script runs before the table exists, exactly as it does while a large
 * preview streams to the browser. NetBox exposes Bootstrap's Modal as a global. */
function loadController() {
  document.body.innerHTML = "";
  window.Modal = Modal;
  if (!controllerLoaded) {
    window.eval(controllerSource);
    controllerLoaded = true;
  }
}

function addRows() {
  document.body.innerHTML = `
    <table><tbody id="previewRowsBody">
      <tr data-action="update">
        <td>
          <button type="button" class="ndi-diff-toggle" data-diff-target="diff-1" aria-expanded="false">
            <i class="mdi mdi-chevron-down"></i> 2 field(s) differ
          </button>
        </td>
      </tr>
      <tr id="diff-1" class="ndi-diff-row" hidden><td>serial</td></tr>
    </tbody></table>
  `;
}

function clickToggle() {
  document.querySelector(".ndi-diff-toggle").dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

/* Two source rows, one of them carrying a difference sub-table with rows of its own,
 * plus an error row: the shape the preview table renders. */
function addFilterRows() {
  document.body.innerHTML = `
    <input type="text" id="previewRowFilter">
    <button id="previewRowFilterClear" style="display:none;">x</button>
    <select id="previewActionFilter"><option value=""></option><option value="update">Update</option>
      <option value="error">Error</option></select>
    <div id="ndi-hidden-err-warn" style="display:none;">
      <span id="ndi-hidden-err-count">0</span>
      <a href="#" id="ndi-show-errors-link">show errors</a>
    </div>
    <table><tbody id="previewRowsBody">
      <tr id="row-1" data-action="update">
        <td>dev-a</td>
        <td>
          <button type="button" class="ndi-diff-toggle" data-diff-target="diff-1" aria-expanded="false">
            <i class="mdi mdi-chevron-down"></i> 1 field(s) differ
          </button>
        </td>
      </tr>
      <tr id="diff-1" class="ndi-diff-row" hidden><td>
        <table class="ndi-diff-table">
          <thead><tr><th>Field</th><th>NetBox</th><th>File</th></tr></thead>
          <tbody><tr id="diff-field-1-serial"><td>serial</td><td>OLD</td><td>NEW</td></tr></tbody>
        </table>
      </td></tr>
      <tr id="row-2" data-action="update"><td>dev-b</td></tr>
      <tr id="row-3" data-action="error"><td>dev-c</td></tr>
    </tbody></table>
    <p id="previewNoFilterResults" style="display:none;">No rows match</p>
  `;
}

function filterBy(text, action = "") {
  const input = document.getElementById("previewRowFilter");
  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  const select = document.getElementById("previewActionFilter");
  select.value = action;
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

beforeEach(() => {
  loadController();
});

describe("preview row controls", () => {
  it("collapses difference rows without the page stylesheet", () => {
    addRows();

    expect(document.getElementById("diff-1").hidden).toBe(true);
  });

  it("expands and collapses rows added after the script ran", () => {
    addRows();
    const diffRow = document.getElementById("diff-1");
    const toggle = document.querySelector(".ndi-diff-toggle");

    clickToggle();

    expect(diffRow.hidden).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(toggle.querySelector(".mdi").classList.contains("mdi-chevron-up")).toBe(true);

    clickToggle();

    expect(diffRow.hidden).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.querySelector(".mdi").classList.contains("mdi-chevron-down")).toBe(true);
  });

  it("collapses one row for the filter and keeps its toggle in step", () => {
    addRows();
    const diffRow = document.getElementById("diff-1");
    clickToggle();

    window.ndiSetDiffExpanded(diffRow, false);

    expect(diffRow.hidden).toBe(true);
    expect(document.querySelector(".ndi-diff-toggle").getAttribute("aria-expanded")).toBe("false");
  });

  it("opens a row modal without a Bootstrap instance per trigger", async () => {
    document.body.innerHTML = `
      <table><tbody id="previewRowsBody">
        <tr><td>
          <button type="button" data-ndi-modal="#conflictModal" data-row-number="4">2 conflicts</button>
        </td></tr>
      </tbody></table>
      <div class="modal" id="conflictModal"><div class="modal-dialog"><div class="modal-content"></div></div></div>
    `;
    const modalElement = document.getElementById("conflictModal");
    const trigger = document.querySelector("[data-ndi-modal]");
    const shown = new Promise((resolveShown) => {
      modalElement.addEventListener("show.bs.modal", (event) => resolveShown(event.relatedTarget));
    });

    expect(Modal.getInstance(modalElement)).toBeNull();

    trigger.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));

    await expect(shown).resolves.toBe(trigger);
    expect(modalElement.classList.contains("show")).toBe(true);
  });

  it("ignores a disabled row button and an unknown modal", () => {
    document.body.innerHTML = `
      <button type="button" data-ndi-modal="#syncRowModal" disabled>Sync to NetBox</button>
      <button type="button" data-ndi-modal="#missingModal">Missing</button>
      <div class="modal" id="syncRowModal"><div class="modal-dialog"><div class="modal-content"></div></div></div>
    `;
    const modalElement = document.getElementById("syncRowModal");

    for (const button of document.querySelectorAll("[data-ndi-modal]")) {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    }

    expect(modalElement.classList.contains("show")).toBe(false);
  });

  it("filters source rows and leaves the rows inside a difference sub-table alone", () => {
    addFilterRows();
    const subTableRow = document.querySelector("#diff-1 .ndi-diff-table tr");

    filterBy("dev-b");

    expect(document.getElementById("row-1").style.display).toBe("none");
    expect(document.getElementById("row-2").style.display).toBe("");
    expect(subTableRow.style.display).toBe("");
    expect(document.getElementById("previewNoFilterResults").style.display).toBe("none");
  });

  it("collapses a difference row when its source row leaves the filter", () => {
    addFilterRows();
    document.querySelector(".ndi-diff-toggle").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(document.getElementById("diff-1").hidden).toBe(false);

    filterBy("dev-b");

    expect(document.getElementById("diff-1").hidden).toBe(true);
    expect(document.querySelector(".ndi-diff-toggle").getAttribute("aria-expanded")).toBe("false");
  });

  it("counts only source rows, so sub-table text cannot hide the empty-result notice", () => {
    addFilterRows();

    // "netbox" appears only in the difference sub-table header.
    filterBy("netbox");

    expect(document.getElementById("previewNoFilterResults").style.display).toBe("");
  });

  it("reports the error rows an action filter hides", () => {
    addFilterRows();

    filterBy("", "update");

    expect(document.getElementById("row-3").style.display).toBe("none");
    expect(document.getElementById("ndi-hidden-err-warn").style.display).toBe("");
    expect(document.getElementById("ndi-hidden-err-count").textContent).toBe("1");

    document.getElementById("ndi-show-errors-link").dispatchEvent(new MouseEvent("click", { bubbles: true }));

    expect(document.getElementById("row-3").style.display).toBe("");
    expect(document.getElementById("row-1").style.display).toBe("none");
  });

  it("clears both filters from the clear button", () => {
    addFilterRows();
    // NetBox turns the action filter into a Tom Select control that holds its own value.
    const actionSelect = new TomSelect(document.getElementById("previewActionFilter"), { create: false });
    actionSelect.setValue("update");
    document.getElementById("previewRowFilter").value = "dev-b";
    document.getElementById("previewRowFilter").dispatchEvent(new Event("input", { bubbles: true }));
    expect(document.getElementById("row-1").style.display).toBe("none");

    document.getElementById("previewRowFilterClear").dispatchEvent(new MouseEvent("click", { bubbles: true }));

    expect(document.getElementById("previewRowFilter").value).toBe("");
    expect(document.getElementById("previewActionFilter").value).toBe("");
    expect(actionSelect.getValue()).toBe("");
    expect(document.getElementById("row-1").style.display).toBe("");
    expect(document.getElementById("row-3").style.display).toBe("");
  });

  it("ignores a toggle whose row is not in the page", () => {
    document.body.innerHTML = `
      <button type="button" class="ndi-diff-toggle" data-diff-target="diff-404">2 field(s) differ</button>
    `;

    expect(() => clickToggle()).not.toThrow();
  });
});

/* Every row expands on a click, so a row with nothing actionable still responds. */
describe("clicking a source row", () => {
  function addRowWithoutAToggle() {
    document.body.innerHTML = `
      <table><tbody id="previewRowsBody">
        <tr id="row-7" data-action="create">
          <td>pw-server-01</td>
          <td><button type="button" class="ndi-sync-btn">Sync</button> <a href="/x">link</a></td>
        </tr>
        <tr id="diff-7" class="ndi-diff-row" hidden><td>nothing to review</td></tr>
        <tr id="row-7" data-action="error">
          <td>duplicate row number</td>
        </tr>
        <tr id="diff-7" class="ndi-diff-row" hidden><td>the second detail row</td></tr>
      </tbody></table>
    `;
  }

  beforeEach(() => {
    loadController();
    addRowWithoutAToggle();
  });

  it("expands the detail row even when the row renders no toggle badge", () => {
    document.getElementById("row-7").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    expect(document.getElementById("diff-7").hidden).toBe(false);
  });

  it("expands the detail row that follows the clicked row, not the first matching id", () => {
    // Row numbers repeat across object types, so the page really does carry duplicate ids.
    const rows = document.querySelectorAll("#previewRowsBody > tr[data-action]");
    rows[1].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const details = document.querySelectorAll("#previewRowsBody > tr.ndi-diff-row");
    expect(details[0].hidden).toBe(true);
    expect(details[1].hidden).toBe(false);
  });

  it("changes only the clicked row's toggle state", () => {
    document.body.innerHTML = `
      <table><tbody id="previewRowsBody">
        <tr id="row-3" data-action="create" aria-expanded="false">
          <td><button type="button" class="ndi-diff-toggle" data-diff-target="diff-1" aria-expanded="false"></button></td>
        </tr>
        <tr id="diff-1" class="ndi-diff-row" hidden><td>first detail</td></tr>
        <tr id="row-3" data-action="error" aria-expanded="false">
          <td><button type="button" class="ndi-diff-toggle" data-diff-target="diff-2" aria-expanded="false"></button></td>
        </tr>
        <tr id="diff-2" class="ndi-diff-row" hidden><td>second detail</td></tr>
      </tbody></table>
    `;
    const rows = document.querySelectorAll("#previewRowsBody > tr[data-action]");
    rows[1].querySelector("td").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const toggles = document.querySelectorAll(".ndi-diff-toggle");
    expect(document.getElementById("diff-1").hidden).toBe(true);
    expect(document.getElementById("diff-2").hidden).toBe(false);
    expect(toggles[0].getAttribute("aria-expanded")).toBe("false");
    expect(toggles[1].getAttribute("aria-expanded")).toBe("true");
  });

  it("collapses again on a second click", () => {
    const row = document.getElementById("row-7");
    row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    expect(document.getElementById("diff-7").hidden).toBe(true);
  });

  it("leaves the detail row alone when a control inside the row is clicked", () => {
    document
      .querySelector("#row-7 .ndi-sync-btn")
      .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    expect(document.getElementById("diff-7").hidden).toBe(true);
  });

  it("leaves the detail row alone when a link inside the row is clicked", () => {
    document
      .querySelector("#row-7 a")
      .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    expect(document.getElementById("diff-7").hidden).toBe(true);
  });
});

/* The script now ships inside the swapped content, so an htmx boost evaluates it again on every
 * navigation. Document listeners outlive the swap, so a second evaluation must not double them. */
describe("evaluating the controller twice", () => {
  beforeEach(() => {
    loadController();
    window.eval(controllerSource);
    document.body.innerHTML = `
      <table><tbody id="previewRowsBody">
        <tr id="row-9" data-action="update" tabindex="0">
          <td>pw-server-09</td>
        </tr>
        <tr id="diff-9" class="ndi-diff-row" hidden><td>detail</td></tr>
      </tbody></table>
    `;
  });

  it("toggles a detail row once per click, not twice", () => {
    document.getElementById("row-9").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    expect(document.getElementById("diff-9").hidden).toBe(false);
  });
});

/* A keyboard-only operator has to reach the same detail rows. */
describe("keyboard activation", () => {
  beforeEach(() => {
    loadController();
    document.body.innerHTML = `
      <table><tbody id="previewRowsBody">
        <tr id="row-11" data-action="update" tabindex="0">
          <td>pw-server-11</td>
          <td><button type="button" class="ndi-sync-btn">Sync</button></td>
        </tr>
        <tr id="diff-11" class="ndi-diff-row" hidden><td>detail</td></tr>
      </tbody></table>
    `;
  });

  function press(key, target) {
    (target || document.getElementById("row-11")).dispatchEvent(
      new window.KeyboardEvent("keydown", { key, bubbles: true }),
    );
  }

  it("expands the detail row on Enter", () => {
    press("Enter");
    expect(document.getElementById("diff-11").hidden).toBe(false);
  });

  it("expands the detail row on Space", () => {
    press(" ");
    expect(document.getElementById("diff-11").hidden).toBe(false);
  });

  it("leaves the detail row alone for a key pressed on a control inside the row", () => {
    press("Enter", document.querySelector("#row-11 .ndi-sync-btn"));
    expect(document.getElementById("diff-11").hidden).toBe(true);
  });
});
