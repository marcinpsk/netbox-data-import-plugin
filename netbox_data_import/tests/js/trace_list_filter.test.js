/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

const filterSource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/trace_list_filter.js",
), "utf8");
const KEY = "ndi.traceWorkspace.dispositionFilter";

/* The Source Traces card as the server renders it: the first trace is the selected one. */
const TRACES = [["T1", "actionable"], ["T2", "blocked"], ["T3", "blocked"], ["T4", "invalid"], ["T5", "no-op"]];

function render({ active = "T1", traces = TRACES } = {}) {
  const counts = { actionable: 1, blocked: 2, invalid: 1, "no-op": 1 };
  document.body.innerHTML = `<div id="page-content">
    <div class="card" data-trace-list-filter>
      <div class="card-body" role="group">
        ${Object.entries(counts).map(([disposition, count]) => `<button type="button" class="btn"
          data-trace-filter="${disposition}" aria-pressed="false">${disposition} ${count}</button>`).join("")}
      </div>
      <div class="list-group">
        ${traces.map(([name, disposition]) => `<a id="${name}" href="?trace=${name}"
          class="list-group-item${name === active ? " active" : ""}" data-disposition="${disposition}">${name}</a>`).join("")}
        <div class="list-group-item" data-trace-filter-empty hidden>No trace matches the filter.</div>
      </div>
    </div>
  </div>`;
}

function load() {
  window.eval(filterSource);
}

function click(disposition) {
  document.querySelector(`[data-trace-filter="${disposition}"]`).click();
}

function visible() {
  return [...document.querySelectorAll("[data-disposition]")].filter((item) => !item.hidden).map((item) => item.id);
}

function pressed() {
  return [...document.querySelectorAll("[data-trace-filter]")]
    .filter((pill) => pill.getAttribute("aria-pressed") === "true")
    .map((pill) => pill.dataset.traceFilter);
}

const emptyLine = () => document.querySelector("[data-trace-filter-empty]");

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  // The document listeners outlive each test, as they outlive an htmx swap, so the flag that guards them stays.
  delete window.ndiTraceListFilterUnsaved;
  document.body.replaceChildren();
});

describe("the Source Trace list filter", () => {
  it("shows every trace and presses no pill when this browser remembers nothing", () => {
    render();
    load();

    expect(visible()).toEqual(["T1", "T2", "T3", "T4", "T5"]);
    expect(pressed()).toEqual([]);
    expect(emptyLine().hidden).toBe(true);
  });

  it("shows only the chosen disposition and the selected trace, and a second click shows all again", () => {
    render();
    load();

    click("blocked");
    expect(pressed()).toEqual(["blocked"]);
    expect(visible()).toEqual(["T1", "T2", "T3"]);

    click("blocked");
    expect(pressed()).toEqual([]);
    expect(visible()).toEqual(["T1", "T2", "T3", "T4", "T5"]);
  });

  it("shows the union of every pressed disposition", () => {
    render({ active: "T2" });
    load();

    click("invalid");
    click("no-op");

    expect(pressed()).toEqual(["invalid", "no-op"]);
    expect(visible()).toEqual(["T2", "T4", "T5"]);
  });

  it("keeps the selected trace visible when its disposition is filtered out", () => {
    render({ active: "T4" });
    load();

    click("actionable");

    expect(visible()).toEqual(["T1", "T4"]);
    expect(document.getElementById("T4").hidden).toBe(false);
  });

  it("shows one muted line when the filter hides every trace", () => {
    render({ active: "", traces: [["T1", "actionable"]] });
    load();

    click("actionable");
    expect(emptyLine().hidden).toBe(true);
    click("actionable");
    click("blocked");

    expect(visible()).toEqual([]);
    expect(emptyLine().hidden).toBe(false);
  });

  it("restores the choice after a refresh", () => {
    render();
    load();
    click("blocked");
    click("invalid");
    expect(JSON.parse(window.localStorage.getItem(KEY))).toEqual(["blocked", "invalid"]);

    // A refresh renders the list unfiltered, and only storage carries the choice.
    render();
    load();

    expect(pressed()).toEqual(["blocked", "invalid"]);
    expect(visible()).toEqual(["T1", "T2", "T3", "T4"]);
  });

  it("ignores a stored value that names no disposition on the page", () => {
    window.localStorage.setItem(KEY, JSON.stringify(["excluded", 7, "blocked"]));
    render();
    load();

    expect(pressed()).toEqual(["blocked"]);
    expect(visible()).toEqual(["T1", "T2", "T3"]);
  });

  it.each(["not json", "{\"blocked\": true}", "\"blocked\""])("shows every trace for the stored value %s", (value) => {
    window.localStorage.setItem(KEY, value);
    render();

    expect(load).not.toThrow();
    expect(pressed()).toEqual([]);
    expect(visible()).toEqual(["T1", "T2", "T3", "T4", "T5"]);
  });

  it("still filters when storage throws on every read and write", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    render();

    expect(load).not.toThrow();
    click("blocked");

    expect(write).toHaveBeenCalledWith(KEY, "[\"blocked\"]");
    expect(visible()).toEqual(["T1", "T2", "T3"]);
  });

  it("applies the filter again after htmx replaces the page content", () => {
    render();
    load();
    click("invalid");

    // The swap brings in the server rendering, which is unfiltered, and the scripts in it have not run yet.
    render({ active: "T3" });
    expect(visible()).toEqual(["T1", "T2", "T3", "T4", "T5"]);
    document.getElementById("page-content").dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true }));

    expect(pressed()).toEqual(["invalid"]);
    expect(visible()).toEqual(["T3", "T4"]);
  });

  it("keeps an unsaved choice across a swap when storage refuses writes", () => {
    window.localStorage.setItem(KEY, JSON.stringify(["no-op"]));
    render();
    load();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });

    click("no-op");
    click("blocked");
    render();
    document.getElementById("page-content").dispatchEvent(new CustomEvent("htmx:afterSettle", { bubbles: true }));

    expect(pressed()).toEqual(["blocked"]);
    expect(visible()).toEqual(["T1", "T2", "T3"]);
  });

  it("adds one click listener however often htmx evaluates the script", () => {
    render();
    load();
    load();

    click("blocked");

    expect(pressed()).toEqual(["blocked"]);
  });
});
