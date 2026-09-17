/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Load the first-party browser asset in jsdom, where session storage answers.
 *
 * The rendered flows live in `tests/browser/sync_row_modal.spec.js`, which runs the same
 * controller in a real browser. This file keeps the cases that need stored state. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const controllerPath = resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/sync_row_modal.js",
);
const controllerSource = readFileSync(controllerPath, "utf8");

const CHOICE_KEY = "ndi-sync-recalculate";

function loadModal() {
  document.body.innerHTML = `
    <div id="syncRowModal" data-sync-url="/sync-single-row/">
      <span id="syncRowName"></span>
      <span id="syncRowNumber"></span>
      <span id="syncRowSourceId"></span>
      <span id="syncRowBadge"></span>
      <p id="syncRowSummary"></p>
      <table>
        <thead><tr><th>Field</th><th id="syncRowCurrentHead"></th><th id="syncRowNextHead"></th></tr></thead>
        <tbody id="syncRowFields"></tbody>
      </table>
      <div><input type="checkbox" id="syncRowShowUnchanged"></div>
      <input type="checkbox" id="syncRowRecalculate" checked>
      <div id="syncRowError" class="d-none"></div>
      <button id="syncRowConfirm">
        <span class="ndi-sync-row-idle">Confirm</span>
        <span class="ndi-sync-row-loading d-none"><span class="ndi-sync-row-loading-label">Syncing</span></span>
      </button>
    </div>
  `;
  window.eval(controllerSource);
  return document.getElementById("syncRowRecalculate");
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  window.sessionStorage.clear();
});

function openRow(entries, dataset) {
  loadModal();
  const blob = document.createElement("script");
  blob.type = "application/json";
  blob.id = "ndi-sync-change-preview-by-row";
  // Keyed by object type and row number together, as the preview page writes it.
  blob.textContent = JSON.stringify(
    entries === null ? {} : { "device:11": entries, 11: [], "rack:11": [] },
  );
  document.body.appendChild(blob);
  const modal = document.getElementById("syncRowModal");
  const trigger = document.createElement("button");
  Object.assign(trigger.dataset, { rowNumber: "11", sourceId: "SRC-1001", objectType: "device" }, dataset);
  document.body.appendChild(trigger);
  modal.dispatchEvent(Object.assign(new Event("show.bs.modal"), { relatedTarget: trigger }));
  return document.getElementById("syncRowFields");
}

const REVIEWED = [
  { field: "u_position", label: "U position", netbox: "5", file: "31", state: "change" },
  { field: "serial", label: "Serial", netbox: "SERIAL-ONE", file: "SERIAL-ONE", state: "unchanged" },
  { field: "status", label: "Status", netbox: "active", file: "active", state: "unchanged" },
  { field: "asset_tag", label: "Asset tag", netbox: "TAG-OLD", file: "TAG-NEW", state: "ignored" },
  { field: "device_name", label: "Name", netbox: "device-one", file: "device-one-from-file", state: "not_written" },
];

describe("the update confirmation", () => {
  it("states what NetBox holds now beside what the sync writes", () => {
    const rows = openRow(REVIEWED, { action: "update" }).querySelectorAll("tr");

    const changed = rows[0].querySelectorAll("td");
    expect(changed[0].textContent).toBe("U position");
    expect(changed[1].textContent).toBe("5");
    expect(changed[2].textContent).toContain("31");
    expect(changed[2].textContent).toContain("will update");
  });

  it("hides the unchanged fields until the operator asks for them", () => {
    const tbody = openRow(REVIEWED, { action: "update" });
    const unchanged = [...tbody.querySelectorAll("tr")].filter((tr) => tr.textContent.includes("unchanged"));

    expect(unchanged).toHaveLength(2);
    expect(unchanged.every((tr) => tr.hidden)).toBe(true);

    const toggle = document.getElementById("syncRowShowUnchanged");
    toggle.checked = true;
    toggle.dispatchEvent(new Event("change"));

    expect(unchanged.every((tr) => tr.hidden)).toBe(false);
  });

  it("counts the changes so the operator sees the size of the write", () => {
    openRow(REVIEWED, { action: "update" });

    // An ignored field and an unwritten one are different states, so one count cannot cover both.
    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("1 field will change. 2 unchanged, 1 ignored, 1 not written.");
  });

  it("says plainly when the write changes nothing", () => {
    openRow(REVIEWED.filter((entry) => entry.state === "unchanged"), { action: "update" });

    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("No field changes. NetBox already holds every reviewed value.");
  });

  it("names each skipped state when nothing changes", () => {
    openRow(REVIEWED.filter((entry) => entry.state !== "change"), { action: "update" });

    // An ignored or unwritten field differs from NetBox, so the value is not already held there.
    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("No fields will change. 2 unchanged, 1 ignored, 1 not written.");
  });

  it("shows an ignored or unwritten value struck through, because it is not applied", () => {
    const tbody = openRow(REVIEWED, { action: "update" });
    const ignored = [...tbody.querySelectorAll("tr")].find((tr) => tr.textContent.includes("ignored"));

    const skipped = ignored.querySelector(".text-decoration-line-through");
    expect(skipped.textContent).toBe("TAG-NEW");
    expect(ignored.querySelectorAll("td")[2].textContent).toContain("OLD");
  });
});

describe("the extra columns", () => {
  it("come from the row's own key, not from another object with the same number", () => {
    loadModal();
    const blob = document.createElement("script");
    blob.type = "application/json";
    blob.id = "ndi-extra-columns-by-row";
    blob.textContent = JSON.stringify({ "device:11": { depth: "508" }, "rack:11": { depth: "WRONG" }, 11: { depth: "ALSO WRONG" } });
    document.body.appendChild(blob);
    const modal = document.getElementById("syncRowModal");
    const trigger = document.createElement("button");
    Object.assign(trigger.dataset, { rowNumber: "11", objectType: "device", action: "create", name: "dev" });
    document.body.appendChild(trigger);
    modal.dispatchEvent(Object.assign(new Event("show.bs.modal"), { relatedTarget: trigger }));

    const cells = [...document.querySelectorAll("#syncRowFields tr")].map((tr) =>
      [...tr.querySelectorAll("td")].map((td) => td.textContent),
    );

    expect(cells).toContainEqual(["depth", "\u2014", "508"]);
    expect(JSON.stringify(cells)).not.toContain("WRONG");
  });
});

describe("the create confirmation", () => {
  it("falls back to the source values, because NetBox holds nothing yet", () => {
    const tbody = openRow(null, { action: "create", name: "new-device", serial: "SER-1" });
    const cells = [...tbody.querySelectorAll("tr")].map((tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent));

    expect(cells).toContainEqual(["Name", "\u2014", "new-device"]);
    expect(document.getElementById("syncRowNextHead").textContent).toBe("Will be set to");
    expect(document.getElementById("syncRowSummary").textContent).toContain("Creates this device");
  });
});

describe("a modal open with no trigger button", () => {
  it("does not confirm the row the previous open left behind", () => {
    loadModal();
    const modal = document.getElementById("syncRowModal");
    const trigger = document.createElement("button");
    trigger.dataset.rowNumber = "17";
    trigger.dataset.sourceId = "SRC-17";
    trigger.dataset.name = "device-a";
    trigger.dataset.objectType = "device";
    document.body.appendChild(trigger);
    modal.dispatchEvent(Object.assign(new Event("show.bs.modal"), { relatedTarget: trigger }));

    const posted = [];
    window.ndiPostPreviewAction = (url, body) => {
      posted.push(body.get("row_number"));
      return Promise.resolve({ message: "Synced." });
    };

    modal.dispatchEvent(new Event("show.bs.modal"));
    document.getElementById("syncRowConfirm").click();

    expect(posted).toEqual([]);
  });
});

describe("the recalculation choice", () => {
  it("is on for an operator who has not chosen", () => {
    expect(loadModal().checked).toBe(true);
  });

  it("stores the choice when the operator clears it", () => {
    const choice = loadModal();

    choice.checked = false;
    choice.dispatchEvent(new Event("change"));

    expect(window.sessionStorage.getItem(CHOICE_KEY)).toBe("off");
  });

  it("holds a cleared choice across the next row the operator opens", () => {
    window.sessionStorage.setItem(CHOICE_KEY, "off");

    expect(loadModal().checked).toBe(false);
  });

  it("comes back on when the operator sets it again", () => {
    window.sessionStorage.setItem(CHOICE_KEY, "off");
    const choice = loadModal();

    choice.checked = true;
    choice.dispatchEvent(new Event("change"));

    expect(window.sessionStorage.getItem(CHOICE_KEY)).toBe("on");
    expect(loadModal().checked).toBe(true);
  });

  it("stays on in a browser that refuses session storage", () => {
    const real = Object.getOwnPropertyDescriptor(window, "sessionStorage");
    Object.defineProperty(window, "sessionStorage", {
      configurable: true,
      get() {
        throw new Error("Storage is disabled.");
      },
    });

    try {
      const choice = loadModal();
      expect(choice.checked).toBe(true);
      // Recording the choice must not throw either, or the change handler kills the modal.
      choice.checked = false;
      expect(() => choice.dispatchEvent(new Event("change"))).not.toThrow();
    } finally {
      if (real) Object.defineProperty(window, "sessionStorage", real);
      else delete window.sessionStorage;
    }
  });
});

describe("a sync that succeeds while another row is still in flight", () => {
  function deferred() {
    let settle = {};
    const promise = new Promise((resolve, reject) => {
      settle = { resolve, reject };
    });
    return { promise, ...settle };
  }

  function openAndConfirm(modal, rowNumber, sourceId) {
    const trigger = document.createElement("button");
    trigger.dataset.rowNumber = String(rowNumber);
    trigger.dataset.sourceId = sourceId;
    trigger.dataset.name = "device-" + rowNumber;
    trigger.dataset.objectType = "device";
    document.body.appendChild(trigger);
    modal.dispatchEvent(Object.assign(new Event("show.bs.modal"), { relatedTarget: trigger }));
    document.getElementById("syncRowConfirm").click();
    return trigger;
  }

  it("recalculates once the last pending request fails", async () => {
    loadModal();
    const modal = document.getElementById("syncRowModal");
    const first = deferred();
    const second = deferred();
    const queue = [first, second];
    window.ndiPostPreviewAction = () => queue.shift().promise;
    window.ndiMarkPreviewStale = () => {};
    let recalculations = 0;
    window.ndiRecalculatePreview = () => {
      recalculations += 1;
      return true;
    };

    openAndConfirm(modal, 17, "SRC-17");
    openAndConfirm(modal, 18, "SRC-18");

    // The write landed, but the second request still holds the preview, so nothing recalculates yet.
    first.resolve({ message: "Synced." });
    await first.promise;
    await Promise.resolve();
    expect(recalculations).toBe(0);

    second.reject(new Error("Sync failed"));
    await second.promise.catch(() => {});
    await Promise.resolve();
    await Promise.resolve();

    expect(recalculations).toBe(1);
  });
});

describe("a failed sync alongside a successful one, with recalculation off", () => {
  function deferred() {
    let settle = {};
    const promise = new Promise((resolve, reject) => {
      settle = { resolve, reject };
    });
    return { promise, ...settle };
  }

  function trigger(rowNumber, sourceId) {
    const button = document.createElement("button");
    button.className = "ndi-sync-row-btn";
    button.dataset.rowNumber = String(rowNumber);
    button.dataset.sourceId = sourceId;
    button.dataset.name = "device-" + rowNumber;
    button.dataset.objectType = "device";
    document.body.appendChild(button);
    return button;
  }

  it("leaves the failed row disabled, because the successful write made the preview stale", async () => {
    const choice = loadModal();
    choice.checked = false;
    choice.dispatchEvent(new Event("change"));
    const modal = document.getElementById("syncRowModal");
    const first = deferred();
    const second = deferred();
    const queue = [first, second];
    window.ndiPostPreviewAction = () => queue.shift().promise;
    window.ndiMarkPreviewStale = () => {};
    window.ndiRecalculatePreview = () => true;

    const buttonA = trigger(17, "SRC-17");
    modal.dispatchEvent(Object.assign(new Event("show.bs.modal"), { relatedTarget: buttonA }));
    document.getElementById("syncRowConfirm").click();
    const buttonB = trigger(18, "SRC-18");
    modal.dispatchEvent(Object.assign(new Event("show.bs.modal"), { relatedTarget: buttonB }));
    document.getElementById("syncRowConfirm").click();

    first.resolve({ message: "Synced." });
    await first.promise;
    await Promise.resolve();

    second.reject(new Error("Sync failed"));
    await second.promise.catch(() => {});
    await Promise.resolve();
    await Promise.resolve();

    // Retrying it would only reach the server's "Recalculate the preview" refusal.
    expect(buttonB.disabled).toBe(true);
    expect(buttonB.title).toBe("Recalculate the preview before synchronizing a row.");
  });
});
