/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Modal } from "bootstrap";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const searchSource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/trace_picker_search.js",
), "utf8");
const controllerSource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/trace_location_picker.js",
), "utf8");

function node(id) {
  return document.getElementById(id);
}

async function openPicker(id = "openFirst") {
  node(id).click();
  await vi.advanceTimersByTimeAsync(0);
  return Array.from(node("traceLocationCandidates").children);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("Modal", Modal);
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({
      ok: true,
      candidates: [
        { id: 4, name: "DH4", parent: "1st Floor" },
        { id: 5, name: "T", parent: "" },
        { id: 6, name: "<img src=x onerror=alert(1)>", parent: "<b>Hall</b>" },
      ],
      shown: 3,
      total: 9,
    }),
  })));
  document.body.innerHTML = `
    <button id="openFirst" data-trace-location-picker="region >> dh4" data-trace-location-label="Region >> DH4">Choose</button>
    <button id="openSecond" data-trace-location-picker="region >> dh5" data-trace-location-label="Region >> DH5">Choose</button>
    <div id="traceLocationPicker" class="modal" tabindex="-1">
      <div class="modal-dialog"><div class="modal-content">
        <form id="traceLocationForm" data-candidates-url="/location-candidates/">
          <input name="preview_revision" value="rev-1">
          <input id="traceLocationKey">
          <input id="traceLocationId">
          <span id="traceLocationLabel"></span>
          <input type="search" id="traceLocationSearch">
          <div id="traceLocationCount" aria-live="polite" hidden></div>
          <div id="traceLocationError" hidden></div>
          <div id="traceLocationCandidates"></div>
          <button type="submit" id="traceLocationSubmit" disabled>Save mapping</button>
        </form>
      </div></div>
    </div>
  `;
  window.eval(searchSource);
  window.eval(controllerSource);
});

afterEach(() => {
  const modal = Modal.getInstance(node("traceLocationPicker"));
  if (modal) {
    modal.hide();
    modal.dispose();
  }
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("trace Location picker", () => {
  it("asks for the path of the row that opened it, with the preview revision", async () => {
    await openPicker("openSecond");

    const url = new URL(fetch.mock.calls[0][0], "http://preview.test");
    expect(url.pathname).toBe("/location-candidates/");
    expect(url.searchParams.get("location_key")).toBe("region >> dh5");
    expect(url.searchParams.get("preview_revision")).toBe("rev-1");
    expect(node("traceLocationKey").value).toBe("region >> dh5");
    expect(node("traceLocationLabel").textContent).toBe("Region >> DH5");
  });

  it("states the count, names a visible parent, and saves only the chosen Location", async () => {
    const candidates = await openPicker();

    expect(node("traceLocationCount").textContent).toBe("3 of 9 visible Locations");
    expect(candidates[0].textContent).toBe("DH4In 1st Floor");
    expect(candidates[1].children).toHaveLength(1);
    expect(node("traceLocationSubmit").disabled).toBe(true);
    candidates[0].click();

    expect(candidates.map(item => item.getAttribute("aria-pressed"))).toEqual(["true", "false", "false"]);
    expect(node("traceLocationId").value).toBe("4");
    expect(node("traceLocationSubmit").disabled).toBe(false);
  });

  it("renders server text as text, never as markup", async () => {
    const candidates = await openPicker();

    expect(candidates[2].querySelector("img, b")).toBeNull();
    expect(candidates[2].textContent).toBe("<img src=x onerror=alert(1)>In <b>Hall</b>");
  });

  it("clears the previous row's choice when another row opens the picker", async () => {
    const candidates = await openPicker();
    candidates[0].click();

    await openPicker("openSecond");

    expect(node("traceLocationId").value).toBe("");
    expect(node("traceLocationSubmit").disabled).toBe(true);
    expect(node("traceLocationKey").value).toBe("region >> dh5");
  });

  it("drops an in-flight search when the operator types again", async () => {
    let release;
    const held = new Promise(resolve => { release = resolve; });
    vi.stubGlobal("fetch", vi.fn(async () => {
      await held;
      return {
        ok: true,
        json: async () => ({ ok: true, candidates: [{ id: 9, name: "Stale", parent: "" }], shown: 1, total: 1 }),
      };
    }));

    node("openFirst").click();
    const search = node("traceLocationSearch");
    search.value = "new search";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    release();
    await vi.advanceTimersByTimeAsync(0);

    expect(Array.from(node("traceLocationCandidates").children)).toEqual([]);
  });

  it("reports a refused lookup and offers nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      json: async () => ({ ok: false, error: "This preview carries no such source Location path." }),
    })));

    const candidates = await openPicker();

    expect(candidates).toEqual([]);
    expect(node("traceLocationError").hidden).toBe(false);
    expect(node("traceLocationError").textContent).toBe("This preview carries no such source Location path.");
    expect(node("traceLocationCount").hidden).toBe(true);
  });
});
