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
  "netbox_data_import/static/netbox_data_import/js/trace_device_picker.js",
), "utf8");

function node(id) {
  return document.getElementById(id);
}

async function openPicker() {
  node("openPicker").click();
  await vi.advanceTimersByTimeAsync(0);
  return Array.from(node("traceDeviceCandidates").children);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("Modal", Modal);
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({
      ok: true,
      candidates: [
        { id: 1, display: "device-a", matched_hints: ["rack", "U position"], conflicting_hints: [] },
        { id: 2, display: "device-b", matched_hints: [], conflicting_hints: ["rack"] },
      ],
      shown: 2,
      total: 2,
    }),
  })));
  document.body.innerHTML = `
    <button id="openPicker" data-trace-device-picker="source-a" data-trace-device-label="Source A">Choose</button>
    <div id="traceDevicePicker" class="modal" tabindex="-1">
      <div class="modal-dialog"><div class="modal-content">
        <form id="traceDeviceForm" data-candidates-url="/device-candidates/">
          <input name="preview_revision" value="1">
          <input id="traceDeviceKey">
          <input id="traceDeviceId">
          <input id="traceDeviceOfferedSearch">
          <span id="traceDeviceLabel"></span>
          <input type="search" id="traceDeviceSearch">
          <div id="traceDeviceCount" aria-live="polite" hidden></div>
          <div id="traceDeviceError" hidden></div>
          <div id="traceDeviceCandidates"></div>
          <button type="submit" id="traceDeviceSubmit" disabled>Save decision</button>
        </form>
      </div></div>
    </div>
  `;
  window.eval(searchSource);
  window.eval(controllerSource);
});

afterEach(() => {
  const modal = Modal.getInstance(node("traceDevicePicker"));
  if (modal) {
    modal.hide();
    modal.dispose();
  }
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("trace Device picker", () => {
  it("shows source-evidence explanations and saves only the selected offer", async () => {
    const candidates = await openPicker();

    expect(candidates[0].textContent).toContain("Matches: rack, U position");
    expect(candidates[1].textContent).toContain("Differs: rack");
    candidates[0].click();

    expect(candidates.map(item => item.getAttribute("aria-pressed"))).toEqual(["true", "false"]);
    expect(node("traceDeviceId").value).toBe("1");
    expect(node("traceDeviceSubmit").disabled).toBe(false);
  });

  it("drops an in-flight search when the operator types again", async () => {
    let release;
    const held = new Promise(resolve => { release = resolve; });
    vi.stubGlobal("fetch", vi.fn(async () => {
      await held;
      return {
        ok: true,
        json: async () => ({
          ok: true,
          candidates: [{ id: 9, display: "stale-device", matched_hints: [], conflicting_hints: [] }],
          shown: 1,
          total: 1,
        }),
      };
    }));

    node("openPicker").click();
    const search = node("traceDeviceSearch");
    search.value = "new search";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    release();
    await vi.advanceTimersByTimeAsync(0);

    expect(Array.from(node("traceDeviceCandidates").children)).toEqual([]);
  });

  it("clears an old offer as soon as the search changes", async () => {
    const candidates = await openPicker();
    candidates[0].click();
    const search = node("traceDeviceSearch");
    search.value = "new search";

    search.dispatchEvent(new Event("input", { bubbles: true }));

    expect(node("traceDeviceId").value).toBe("");
    expect(node("traceDeviceOfferedSearch").value).toBe("");
    expect(node("traceDeviceSubmit").disabled).toBe(true);
  });
});
