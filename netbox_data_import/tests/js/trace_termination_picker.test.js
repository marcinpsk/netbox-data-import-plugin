/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Modal } from "bootstrap";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const controllerSource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/trace_termination_picker.js",
), "utf8");
const templateSource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/templates/netbox_data_import/trace_workspace.html",
), "utf8");

function node(id) {
  return document.getElementById(id);
}

async function openPicker() {
  node("openPicker").click();
  await vi.advanceTimersByTimeAsync(0);
  return Array.from(node("traceTerminationCandidates").children);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("Modal", Modal);
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({
      ok: true,
      candidates: [{ id: 1, display: "port-a" }, { id: 2, display: "port-b" }],
      shown: 2,
      total: 2,
    }),
  })));
  document.body.innerHTML = `
    <button id="openPicker" data-trace-picker="termination" data-trace-kind="interface">Choose</button>
    <div id="traceTerminationPicker" class="modal" tabindex="-1">
      <div class="modal-dialog"><div class="modal-content">
        <form id="traceTerminationForm" data-candidates-url="/candidates/">
          <input name="preview_revision" value="1">
          <input id="traceTerminationFieldKey">
          <input id="traceTerminationObjectId">
          <input id="traceTerminationObjectType">
          <input id="traceTerminationOfferedSearch">
          <span id="traceTerminationLabel"></span>
          <input type="search" id="traceTerminationSearch">
          ${templateSource.match(/<div\b[^>]*\bid="traceTerminationCount"[^>]*><\/div>/)[0]}
          <div id="traceTerminationError" hidden></div>
          <div id="traceTerminationCandidates"></div>
          <button type="submit" id="traceTerminationSubmit" disabled>Save decision</button>
        </form>
      </div></div>
    </div>
  `;
  window.eval(controllerSource);
});

afterEach(() => {
  const modal = Modal.getInstance(node("traceTerminationPicker"));
  if (modal) {
    modal.hide();
    modal.dispose();
  }
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("trace termination picker", () => {
  it("marks only the selected candidate as pressed", async () => {
    const candidates = await openPicker();
    candidates[0].click();

    expect(candidates.map(item => item.getAttribute("aria-pressed"))).toEqual(["true", "false"]);
    expect(candidates.map(item => item.classList.contains("active"))).toEqual([true, false]);
    expect(node("traceTerminationObjectId").value).toBe("1");
  });

  it("moves the pressed state when another candidate is selected", async () => {
    const candidates = await openPicker();
    candidates[0].click();
    candidates[1].click();

    expect(candidates.map(item => item.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
    expect(candidates.map(item => item.classList.contains("active"))).toEqual([false, true]);
    expect(node("traceTerminationObjectId").value).toBe("2");
  });

  it("resets every pressed state when a search clears the selection", async () => {
    const candidates = await openPicker();
    candidates[0].click();
    node("traceTerminationSearch").value = "port";
    node("traceTerminationSearch").dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(200);

    expect(candidates.map(item => item.getAttribute("aria-pressed"))).toEqual(["false", "false"]);
    expect(Array.from(node("traceTerminationCandidates").children, item => item.getAttribute("aria-pressed")))
      .toEqual(["false", "false"]);
    expect(node("traceTerminationObjectId").value).toBe("");
    expect(node("traceTerminationObjectType").value).toBe("");
    expect(node("traceTerminationOfferedSearch").value).toBe("");
    expect(node("traceTerminationSubmit").disabled).toBe(true);
  });

  it("drops the selection the moment the search changes, before the lookup returns", async () => {
    const candidates = await openPicker();
    candidates[0].click();
    expect(node("traceTerminationSubmit").disabled).toBe(false);
    const search = node("traceTerminationSearch");
    search.value = "different";

    search.dispatchEvent(new Event("input", { bubbles: true }));

    expect(node("traceTerminationSubmit").disabled).toBe(true);
    expect(node("traceTerminationObjectId").value).toBe("");
    expect(node("traceTerminationObjectType").value).toBe("");
    expect(node("traceTerminationOfferedSearch").value).toBe("");
  });

  it("declares the candidate count as a polite live region in the template", async () => {
    expect(node("traceTerminationCount").getAttribute("aria-live")).toBe("polite");
    await openPicker();
    expect(node("traceTerminationCount").textContent).toBe("2 of 2 eligible");
    expect(node("traceTerminationCount").hidden).toBe(false);
  });

  it("cancels Enter submission after editing the search with a previous candidate selected", async () => {
    const candidates = await openPicker();
    candidates[0].click();
    expect(node("traceTerminationSubmit").disabled).toBe(false);
    const search = node("traceTerminationSearch");
    search.value = "different";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });

    search.dispatchEvent(enter);

    expect(enter.defaultPrevented).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
