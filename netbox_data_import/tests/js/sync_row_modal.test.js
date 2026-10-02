/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Load the first-party browser assets in jsdom: the modal, and the row-action script that posts
 * the Preview Claim and reloads the page.
 *
 * The rendered flows live in `tests/browser/sync_row_modal.spec.js`, which runs the same
 * controllers in a real browser. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const scriptDir = resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js");
const controllerSource = readFileSync(resolve(scriptDir, "sync_row_modal.js"), "utf8");
const claimSource = readFileSync(resolve(scriptDir, "preview_claim.js"), "utf8");
const actionsSource = readFileSync(resolve(scriptDir, "preview_row_actions.js"), "utf8");

const CLAIM = [
  ["preview_token", "token-1"],
  ["preview_revision", "4"],
  ["preview_document", "11"],
  ["preview_profile", "3"],
];

/* The row-action script delegates from `document`, so it is loaded once per file. */
let postPreviewAction = null;

function loadModal() {
  const claim = CLAIM.map(([name, value]) => `<input type="hidden" name="${name}" value="${value}">`).join("");
  document.body.innerHTML = `
    <input type="hidden" name="csrfmiddlewaretoken" value="test-token">
    <form id="ndi-preview-claim" hidden>${claim}</form>
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
      <div id="syncRowError" class="d-none"></div>
      <button id="syncRowConfirm">
        <span class="ndi-sync-row-idle">Confirm</span>
        <span class="ndi-sync-row-loading d-none"><span class="ndi-sync-row-loading-label">Syncing</span></span>
      </button>
    </div>
  `;
  if (!postPreviewAction) {
    window.eval(claimSource);
    window.eval(actionsSource);
    postPreviewAction = window.ndiPostPreviewAction;
  }
  window.eval(controllerSource);
}

let reload;

beforeEach(() => {
  reload = vi.fn();
  vi.stubGlobal("location", { reload });
});

afterEach(() => {
  vi.unstubAllGlobals();
  if (postPreviewAction) window.ndiPostPreviewAction = postPreviewAction;
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

  it("qualifies the zero-change summary when no other write is pending", () => {
    openRow(REVIEWED.filter((entry) => entry.state === "unchanged"), { action: "update" });

    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("No reviewed device fields will change. NetBox already holds every reviewed value.");
  });

  it("names a pending contact write when reviewed fields do not change", () => {
    openRow(REVIEWED.filter((entry) => entry.state === "unchanged"), {
      action: "update",
      pendingWriteContact: "true",
    });

    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("No reviewed device fields will change. Sync will also write contact data.");
  });

  it("names a pending provenance write when reviewed fields do not change", () => {
    openRow(REVIEWED.filter((entry) => entry.state === "unchanged"), {
      action: "update",
      pendingWriteProvenance: "true",
    });

    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("No reviewed device fields will change. Sync will also write provenance data.");
  });

  it("names a pending contact write when reviewed fields also change", () => {
    openRow(REVIEWED, { action: "update", pendingWriteContact: "true" });

    // The confirmation must not report fewer writes than the sync performs.
    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("1 field will change. 2 unchanged, 1 ignored, 1 not written. Sync will also write contact data.");
  });

  it("names a pending provenance write when the row creates the object", () => {
    openRow(null, { action: "create", name: "new-device", pendingWriteProvenance: "true" });

    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("Creates this device in NetBox with the values below. Sync will also write provenance data.");
  });

  it("states that a rack update writes, because a rack carries no field review", () => {
    openRow(REVIEWED, { action: "update", objectType: "rack", rackName: "rack-one" });

    // Only a Device row is field-reviewed, so an empty preview here is a rack update, not a no-op.
    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("Updates this rack in NetBox with the values below.");
  });

  it("names each skipped state when nothing changes", () => {
    openRow(REVIEWED.filter((entry) => entry.state !== "change"), { action: "update" });

    // An ignored or unwritten field differs from NetBox, so the value is not already held there.
    expect(document.getElementById("syncRowSummary").textContent)
      .toBe("No reviewed device fields will change. 2 unchanged, 1 ignored, 1 not written.");
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

describe("confirming a sync", () => {
  function deferred() {
    let settle = {};
    const promise = new Promise((resolve, reject) => {
      settle = { resolve, reject };
    });
    return { promise, ...settle };
  }

  function json(status, payload) {
    return { ok: status < 400, status, json: () => Promise.resolve(payload) };
  }

  const REPLANNED = { ok: true, preview_state: "replanned", row_number: 17, message: "Synced.", detail: "" };

  function openAndConfirm(rowNumber) {
    const modal = document.getElementById("syncRowModal");
    const trigger = document.createElement("button");
    trigger.className = "ndi-sync-row-btn";
    trigger.dataset.rowNumber = String(rowNumber);
    trigger.dataset.sourceId = "SRC-" + rowNumber;
    trigger.dataset.name = "device-" + rowNumber;
    trigger.dataset.objectType = "device";
    document.body.appendChild(trigger);
    modal.dispatchEvent(Object.assign(new Event("show.bs.modal"), { relatedTarget: trigger }));
    document.getElementById("syncRowConfirm").click();
    return trigger;
  }

  function settle() {
    return new Promise((resolveTick) => setTimeout(resolveTick, 0));
  }

  it("posts the row with the page claim and reloads the replanned preview", async () => {
    loadModal();
    const fetchMock = vi.fn(() => Promise.resolve(json(200, REPLANNED)));
    vi.stubGlobal("fetch", fetchMock);

    openAndConfirm(17);
    await settle();

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/sync-single-row/");
    expect([...options.body]).toEqual([["row_number", "17"], ...CLAIM]);
    expect(reload).toHaveBeenCalledOnce();
    // The page is leaving, so the modal keeps reporting the wait instead of offering a second sync.
    expect(document.getElementById("syncRowConfirm").disabled).toBe(true);
    expect(document.querySelector(".ndi-sync-row-loading-label").textContent).toBe("Reloading preview…");
  });

  it("shows a stale claim refusal in the modal and does not reload", async () => {
    loadModal();
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(
      json(409, { ok: false, error: "A newer preview replaced this one.", code: "preview_stale" }),
    )));

    const trigger = openAndConfirm(17);
    await settle();

    const error = document.getElementById("syncRowError");
    expect(error.textContent).toBe("A newer preview replaced this one.");
    expect(error.classList.contains("d-none")).toBe(false);
    expect(document.getElementById("syncRowConfirm").disabled).toBe(false);
    expect(trigger.disabled).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });

  it("reloads once when the syncs of two rows both succeed", async () => {
    loadModal();
    const first = deferred();
    const second = deferred();
    const queue = [first, second];
    vi.stubGlobal("fetch", vi.fn(() => queue.shift().promise));

    openAndConfirm(17);
    openAndConfirm(18);
    first.resolve(json(200, REPLANNED));
    await settle();
    second.resolve(json(200, { ...REPLANNED, row_number: 18 }));
    await settle();

    expect(reload).toHaveBeenCalledOnce();
  });

  it("keeps the reload of a successful row when another row fails", async () => {
    loadModal();
    const first = deferred();
    const second = deferred();
    const queue = [first, second];
    vi.stubGlobal("fetch", vi.fn(() => queue.shift().promise));

    openAndConfirm(17);
    const failed = openAndConfirm(18);
    first.resolve(json(200, REPLANNED));
    await settle();
    second.resolve(json(409, { ok: false, error: "A newer preview replaced this one.", code: "preview_stale" }));
    await settle();

    expect(reload).toHaveBeenCalledOnce();
    expect(document.getElementById("syncRowError").textContent).toBe("A newer preview replaced this one.");
    expect(failed.disabled).toBe(false);
  });
});
