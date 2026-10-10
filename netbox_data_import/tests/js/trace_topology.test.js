/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

const topologySource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/trace_topology.js",
), "utf8");
const KEY = "ndi.traceWorkspace.topologyOpen";

function render() {
  document.body.innerHTML = `<div id="page-content">
    <details data-trace-topology><summary>Topology</summary><p>panels</p></details>
  </div>`;
}

function group() {
  return document.querySelector("[data-trace-topology]");
}

function load() {
  window.eval(topologySource);
}

async function toggle() {
  group().open = !group().open;
  // The browser queues the toggle event as a task.
  await new Promise((done) => setTimeout(done, 0));
}

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  delete window.ndiTraceTopology;
  delete window.ndiTraceTopologyOpen;
  document.body.replaceChildren();
});

describe("the trace topology group", () => {
  it("stays closed when this browser remembers nothing", () => {
    render();
    load();

    expect(group().open).toBe(false);
  });

  it("remembers that the viewer opened it and opens it on the next page", async () => {
    render();
    load();

    await toggle();
    expect(window.localStorage.getItem(KEY)).toBe("true");

    render();
    load();
    expect(group().open).toBe(true);
  });

  it("remembers that the viewer closed it again", async () => {
    window.localStorage.setItem(KEY, "true");
    render();
    load();

    await toggle();

    expect(window.localStorage.getItem(KEY)).toBe("false");
  });

  it("opens the group again after htmx swaps the workspace", async () => {
    window.localStorage.setItem(KEY, "true");
    render();
    load();
    // A swap brings in the server default, which is closed.
    render();
    expect(group().open).toBe(false);

    document.getElementById("page-content").dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));

    expect(group().open).toBe(true);
  });

  it("keeps the server default when storage throws on every read and write", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    render();

    expect(load).not.toThrow();
    expect(group().open).toBe(false);
    await toggle();

    expect(write).toHaveBeenCalledWith(KEY, "true");
    expect(group().open).toBe(true);
  });

  it("keeps the viewer's choice when a card refresh loads and storage refuses writes", async () => {
    window.localStorage.setItem(KEY, "false");
    render();
    document.getElementById("page-content").insertAdjacentHTML("beforeend", '<li id="card">card</li>');
    load();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });

    await toggle();
    document.getElementById("card").dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));

    expect(group().open).toBe(true);
  });

  it("leaves the group alone when a card refresh lands before the toggle event", async () => {
    window.localStorage.setItem(KEY, "false");
    render();
    document.getElementById("page-content").insertAdjacentHTML("beforeend", '<li id="card">card</li>');
    load();

    group().open = true;
    document.getElementById("card").dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
    await new Promise((done) => setTimeout(done, 0));

    expect([group().open, window.localStorage.getItem(KEY)]).toEqual([true, "true"]);
  });

  it("keeps the viewer's choice across a workspace swap when storage refuses writes", async () => {
    window.localStorage.setItem(KEY, "false");
    render();
    load();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });

    await toggle();
    render();
    load();
    document.getElementById("page-content").dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));

    expect(group().open).toBe(true);
  });

  it("ignores a value it did not write", () => {
    window.localStorage.setItem(KEY, "yes");
    render();
    load();

    expect(group().open).toBe(false);
  });
});
