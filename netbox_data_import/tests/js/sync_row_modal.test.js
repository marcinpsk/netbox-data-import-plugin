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
      <table><tbody id="syncRowFields"></tbody></table>
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
