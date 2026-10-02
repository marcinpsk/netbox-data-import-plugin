/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Modal } from "bootstrap";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const pickerSource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/trace_picker.js",
), "utf8");
const claimSource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/preview_claim.js",
), "utf8");
const templateSource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/templates/netbox_data_import/trace_workspace.html",
), "utf8");

const CLAIM = [
  ["preview_token", "token-1"],
  ["preview_revision", "4"],
  ["preview_document", "11"],
  ["preview_profile", "3"],
];
const claimInputs = CLAIM.map(([name, value]) => `<input type="hidden" name="${name}" value="${value}">`).join("");

function node(id) {
  return document.getElementById(id);
}

function fromTemplate(pattern) {
  return templateSource.match(pattern)[0];
}

// The count and the page navigation come from the template, so the fixture cannot drift from it.
function dialog(prefix, url, hiddenIds) {
  return `
    <div id="${prefix}Picker" class="modal" tabindex="-1">
      <div class="modal-dialog"><div class="modal-content">
        <form id="${prefix}Form" data-candidates-url="${url}">
          ${claimInputs}
          ${hiddenIds.map(id => `<input id="${prefix}${id}">`).join("")}
          <span id="${prefix}Label"></span>
          <input type="search" id="${prefix}Search">
          ${fromTemplate(new RegExp(`<div\\b[^>]*\\bid="${prefix}Count"[^>]*></div>`))}
          <div id="${prefix}Error" hidden></div>
          <div id="${prefix}Candidates"></div>
          ${fromTemplate(new RegExp(`<nav\\b[^>]*\\bid="${prefix}Pages"[\\s\\S]*?</nav>`))}
          <button type="submit" id="${prefix}Submit" disabled>Save</button>
        </form>
      </div></div>
    </div>`;
}

function answer(payload) {
  return { ok: true, json: async () => ({ ok: true, offset: 0, limit: 20, ...payload }) };
}

function serve(payload) {
  vi.stubGlobal("fetch", vi.fn(async () => answer(payload)));
}

function rows(prefix) {
  return Array.from(node(`${prefix}Candidates`).children);
}

async function open(trigger, prefix) {
  node(trigger).click();
  await vi.advanceTimersByTimeAsync(0);
  return rows(prefix);
}

function type(prefix, text) {
  const search = node(`${prefix}Search`);
  search.value = text;
  search.dispatchEvent(new Event("input", { bubbles: true }));
  return search;
}

function asked(call = -1) {
  return new URL(fetch.mock.calls.at(call)[0], "http://preview.test");
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("Modal", Modal);
  serve({ candidates: [], shown: 0, total: 0 });
  document.body.innerHTML = `
    <form id="ndi-preview-claim" hidden>${claimInputs}</form>
    <button id="openTermination" data-trace-picker="termination" data-trace-label="DEV-A port">Choose</button>
    <button id="openDevice" data-trace-device-picker="source alias" data-trace-device-label="Source Alias">Choose</button>
    <button id="openFirst" data-trace-location-picker="region >> dh4" data-trace-location-label="Region >> DH4">Choose</button>
    <button id="openSecond" data-trace-location-picker="region >> dh5" data-trace-location-label="Region >> DH5">Choose</button>
    ${dialog("traceTermination", "/candidates/", ["Key", "ObjectId", "ObjectType", "OfferedSearch", "OfferedOffset"])}
    ${dialog("traceDevice", "/device-candidates/", ["Key", "Id", "OfferedSearch", "OfferedOffset"])}
    ${dialog("traceLocation", "/location-candidates/", ["Key", "Id"])}
  `;
  // The script guards against a second evaluation, as an htmx boost would cause.
  window.eval(claimSource);
  window.eval(pickerSource);
});

afterEach(() => {
  for (const prefix of ["traceTermination", "traceDevice", "traceLocation"]) {
    const modal = Modal.getInstance(node(`${prefix}Picker`));
    if (modal) {
      modal.hide();
      modal.dispose();
    }
  }
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("one picker behavior for every trace question", () => {
  it.each([
    ["openTermination", "traceTermination", "field_key", "termination", "/candidates/"],
    ["openDevice", "traceDevice", "device_key", "source alias", "/device-candidates/"],
    ["openSecond", "traceLocation", "location_key", "region >> dh5", "/location-candidates/"],
  ])("%s asks its own endpoint for the question of the row that opened it", async (trigger, prefix, key, value, path) => {
    await open(trigger, prefix);

    expect(asked().pathname).toBe(path);
    expect(asked().searchParams.get(key)).toBe(value);
    expect(asked().searchParams.get("offset")).toBe("0");
    expect(CLAIM.map(([name]) => [name, asked().searchParams.get(name)])).toEqual(CLAIM);
    expect(node(`${prefix}Key`).value).toBe(value);
  });

  it.each([
    ["openTermination", "traceTermination"],
    ["openDevice", "traceDevice"],
    ["openFirst", "traceLocation"],
  ])("%s asks nothing on a page that holds no preview claim", async (trigger, prefix) => {
    node("ndi-preview-claim").remove();

    await open(trigger, prefix);

    expect(fetch).not.toHaveBeenCalled();
    expect(node(`${prefix}Error`).hidden).toBe(false);
    expect(node(`${prefix}Error`).textContent).toBe("The page holds no preview claim. Reload the page.");
  });

  it.each([
    ["openTermination", "traceTermination"],
    ["openDevice", "traceDevice"],
    ["openFirst", "traceLocation"],
  ])("%s drops an in-flight search when the operator types again", async (trigger, prefix) => {
    let release;
    const held = new Promise(done => { release = done; });
    vi.stubGlobal("fetch", vi.fn(async () => {
      await held;
      return answer({ candidates: [{ id: 9, name: "stale", display: "stale" }], shown: 1, total: 1 });
    }));

    node(trigger).click();
    type(prefix, "new search");
    release();
    await vi.advanceTimersByTimeAsync(0);

    expect(rows(prefix)).toEqual([]);
  });

  it.each([
    ["openTermination", "traceTermination"],
    ["openDevice", "traceDevice"],
    ["openFirst", "traceLocation"],
  ])("%s pages forward and back, and keeps the search it pages through", async (trigger, prefix) => {
    serve({ candidates: [{ id: 1, name: "Room", display: "Room" }], shown: 20, total: 21 });
    await open(trigger, prefix);
    type(prefix, "Room");
    await vi.advanceTimersByTimeAsync(200);
    expect(node(`${prefix}Pages`).hidden).toBe(false);
    expect(node(`${prefix}Previous`).disabled).toBe(true);
    expect(node(`${prefix}Next`).disabled).toBe(false);
    serve({ candidates: [{ id: 21, name: "Room", display: "Room" }], shown: 1, total: 21, offset: 20 });

    node(`${prefix}Next`).click();
    await vi.advanceTimersByTimeAsync(0);

    expect(asked().searchParams.get("offset")).toBe("20");
    expect(asked().searchParams.get("search")).toBe("Room");
    expect(node(`${prefix}Count`).textContent).toMatch(/^21–21 of 21 /);
    expect(node(`${prefix}Previous`).disabled).toBe(false);
    expect(node(`${prefix}Next`).disabled).toBe(true);
    serve({ candidates: [{ id: 1, name: "Room", display: "Room" }], shown: 20, total: 21 });

    node(`${prefix}Previous`).click();
    await vi.advanceTimersByTimeAsync(0);

    expect(asked().searchParams.get("offset")).toBe("0");
    expect(node(`${prefix}Count`).textContent).toMatch(/^20 of 21 /);
  });

  it.each([
    ["openTermination", "traceTermination"],
    ["openDevice", "traceDevice"],
    ["openFirst", "traceLocation"],
  ])("%s hides the page navigation when one page holds every candidate", async (trigger, prefix) => {
    serve({ candidates: [{ id: 1, name: "only", display: "only" }], shown: 1, total: 1 });

    await open(trigger, prefix);

    expect(node(`${prefix}Pages`).hidden).toBe(true);
  });

  it.each([
    ["openTermination", "traceTermination"],
    ["openDevice", "traceDevice"],
    ["openFirst", "traceLocation"],
  ])("%s reports a refused lookup and offers nothing", async (trigger, prefix) => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      json: async () => ({ ok: false, error: "This preview asked no such question." }),
    })));

    const candidates = await open(trigger, prefix);

    expect(candidates).toEqual([]);
    expect(node(`${prefix}Error`).hidden).toBe(false);
    expect(node(`${prefix}Error`).textContent).toBe("This preview asked no such question.");
    expect(node(`${prefix}Count`).hidden).toBe(true);
    expect(node(`${prefix}Pages`).hidden).toBe(true);
  });

  it.each([
    ["openTermination", "traceTermination"],
    ["openDevice", "traceDevice"],
    ["openFirst", "traceLocation"],
  ])("%s offers no page turn while a typed search waits for its answer", async (trigger, prefix) => {
    serve({ candidates: [{ id: 1, name: "Room", display: "Room" }], shown: 20, total: 40 });
    await open(trigger, prefix);
    expect(node(`${prefix}Next`).disabled).toBe(false);

    type(prefix, "Spare");

    expect(node(`${prefix}Next`).disabled).toBe(true);
    expect(node(`${prefix}Previous`).disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(200);
    expect(asked().searchParams.get("search")).toBe("Spare");
    expect(asked().searchParams.get("offset")).toBe("0");
    expect(node(`${prefix}Next`).disabled).toBe(false);
  });

  it("a page turn drops the selection of the page it leaves", async () => {
    serve({ candidates: [{ id: 3, display: "port-a", object_type: "dcim.interface" }], shown: 20, total: 25 });
    const candidates = await open("openTermination", "traceTermination");
    candidates[0].click();
    expect(node("traceTerminationSubmit").disabled).toBe(false);

    node("traceTerminationNext").click();

    expect(node("traceTerminationSubmit").disabled).toBe(true);
    expect(node("traceTerminationObjectId").value).toBe("");
    expect(node("traceTerminationOfferedOffset").value).toBe("");
  });
});

describe("trace termination picker", () => {
  beforeEach(() => {
    serve({
      candidates: [
        { id: 1, object_type: "dcim.interface", model: "interface", display: "port-a" },
        { id: 2, object_type: "dcim.interface", model: "interface", display: "port-b" },
      ],
      shown: 2,
      total: 2,
    });
  });

  it("marks only the selected candidate as pressed", async () => {
    const candidates = await open("openTermination", "traceTermination");
    candidates[0].click();

    expect(candidates.map(item => item.getAttribute("aria-pressed"))).toEqual(["true", "false"]);
    expect(candidates.map(item => item.classList.contains("active"))).toEqual([true, false]);
    expect(node("traceTerminationObjectId").value).toBe("1");
  });

  it("moves the pressed state when another candidate is selected", async () => {
    const candidates = await open("openTermination", "traceTermination");
    candidates[0].click();
    candidates[1].click();

    expect(candidates.map(item => item.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
    expect(candidates.map(item => item.classList.contains("active"))).toEqual([false, true]);
    expect(node("traceTerminationObjectId").value).toBe("2");
  });

  it("resets every pressed state when a search clears the selection", async () => {
    const candidates = await open("openTermination", "traceTermination");
    candidates[0].click();
    type("traceTermination", "port");
    await vi.advanceTimersByTimeAsync(200);

    expect(candidates.map(item => item.getAttribute("aria-pressed"))).toEqual(["false", "false"]);
    expect(rows("traceTermination").map(item => item.getAttribute("aria-pressed"))).toEqual(["false", "false"]);
    expect(node("traceTerminationObjectId").value).toBe("");
    expect(node("traceTerminationObjectType").value).toBe("");
    expect(node("traceTerminationOfferedSearch").value).toBe("");
    expect(node("traceTerminationSubmit").disabled).toBe(true);
  });

  it("drops the selection the moment the search changes, before the lookup returns", async () => {
    const candidates = await open("openTermination", "traceTermination");
    candidates[0].click();
    expect(node("traceTerminationSubmit").disabled).toBe(false);

    type("traceTermination", "different");

    expect(node("traceTerminationSubmit").disabled).toBe(true);
    expect(node("traceTerminationObjectId").value).toBe("");
    expect(node("traceTerminationObjectType").value).toBe("");
    expect(node("traceTerminationOfferedSearch").value).toBe("");
  });

  it("declares the candidate count as a polite live region in the template", async () => {
    expect(node("traceTerminationCount").getAttribute("aria-live")).toBe("polite");
    await open("openTermination", "traceTermination");
    expect(node("traceTerminationCount").textContent).toBe("2 of 2 eligible");
    expect(node("traceTerminationCount").hidden).toBe(false);
  });

  it("submits the object type of the clicked candidate when two candidates share one id", async () => {
    serve({
      candidates: [
        { id: 7, object_type: "dcim.interface", model: "interface", display: "eth7" },
        { id: 7, object_type: "dcim.powerport", model: "power port", display: "PSU1" },
      ],
      shown: 2,
      total: 2,
    });
    const candidates = await open("openTermination", "traceTermination");

    candidates[1].click();

    expect(node("traceTerminationObjectId").value).toBe("7");
    expect(node("traceTerminationObjectType").value).toBe("dcim.powerport");
    expect(node("traceTerminationSubmit").disabled).toBe(false);
    candidates[0].click();
    expect(node("traceTerminationObjectType").value).toBe("dcim.interface");
  });

  it("submits the search and the page offset that made the offer", async () => {
    serve({ candidates: [{ id: 1, object_type: "dcim.interface", display: "spare-01" }], shown: 20, total: 30 });
    await open("openTermination", "traceTermination");
    type("traceTermination", "spare");
    await vi.advanceTimersByTimeAsync(200);
    serve({ candidates: [{ id: 30, object_type: "dcim.interface", display: "spare-30" }], shown: 10, total: 30, offset: 20 });
    node("traceTerminationNext").click();
    await vi.advanceTimersByTimeAsync(0);
    // The box changes without a search, so only the answered query may travel with the offer.
    node("traceTerminationSearch").value = "unsent";

    rows("traceTermination")[0].click();

    expect(node("traceTerminationObjectId").value).toBe("30");
    expect(node("traceTerminationOfferedSearch").value).toBe("spare");
    expect(node("traceTerminationOfferedOffset").value).toBe("20");
  });

  it("names the model of every candidate it offers", async () => {
    serve({
      candidates: [{ id: 3, object_type: "dcim.consoleport", model: "console port", display: "con0" }],
      shown: 1,
      total: 1,
    });

    const candidates = await open("openTermination", "traceTermination");

    expect(candidates.map(item => item.textContent)).toEqual(["con0 console port"]);
  });

  it("refuses to submit a candidate the server sent without an object type", async () => {
    serve({ candidates: [{ id: 4, display: "untyped" }], shown: 1, total: 1 });
    const candidates = await open("openTermination", "traceTermination");

    candidates[0].click();

    expect(node("traceTerminationObjectType").value).toBe("");
    expect(node("traceTerminationSubmit").disabled).toBe(true);
  });

  it("cancels Enter submission after editing the search with a previous candidate selected", async () => {
    const candidates = await open("openTermination", "traceTermination");
    candidates[0].click();
    expect(node("traceTerminationSubmit").disabled).toBe(false);
    const search = type("traceTermination", "different");
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });

    search.dispatchEvent(enter);

    expect(enter.defaultPrevented).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe("trace Device picker", () => {
  beforeEach(() => {
    serve({
      candidates: [
        {
          id: 1,
          display: "device-a",
          matched_facts: [
            { fact: "rack", source: "Rack A", mapped: "", netbox: "Rack A" },
            { fact: "location", source: "Region >> DH4", mapped: "DH4", netbox: "Row T" },
          ],
          conflicting_facts: [],
          import_location: { location: "1st Floor", netbox: "Row T" },
        },
        {
          id: 2,
          display: "device-b",
          matched_facts: [],
          conflicting_facts: [{ fact: "location", source: "Region >> DH4", mapped: "DH4", netbox: "DH5" }],
          import_location: null,
        },
        {
          id: 3,
          display: "<img src=x onerror=alert(1)>",
          matched_facts: [{ fact: "rack", source: "<b>Rack</b>", mapped: "", netbox: "<i>Rack</i>" }],
          conflicting_facts: [],
          import_location: null,
        },
      ],
      shown: 3,
      total: 3,
    });
  });

  it("names both values of each fact and saves only the selected offer", async () => {
    const candidates = await open("openDevice", "traceDevice");
    const lines = item => Array.from(item.children).slice(1).map(line => line.textContent);

    expect(lines(candidates[0])).toEqual([
      "Matches rack: source Rack A, NetBox Rack A",
      "Matches location: source Region >> DH4, mapped to DH4, NetBox Row T",
      "In import Location 1st Floor (NetBox Row T)",
    ]);
    expect(lines(candidates[1])).toEqual(["Differs location: source Region >> DH4, mapped to DH4, NetBox DH5"]);
    candidates[0].click();

    expect(candidates.map(item => item.getAttribute("aria-pressed"))).toEqual(["true", "false", "false"]);
    expect(node("traceDeviceId").value).toBe("1");
    expect(node("traceDeviceOfferedOffset").value).toBe("0");
    expect(node("traceDeviceSubmit").disabled).toBe(false);
  });

  it("renders server text as text, never as markup", async () => {
    const candidates = await open("openDevice", "traceDevice");

    expect(candidates[2].querySelector("img, b, i")).toBeNull();
    expect(candidates[2].textContent).toContain("<img src=x onerror=alert(1)>");
    expect(candidates[2].textContent).toContain("Matches rack: source <b>Rack</b>, NetBox <i>Rack</i>");
  });

  it("clears an old offer as soon as the search changes", async () => {
    const candidates = await open("openDevice", "traceDevice");
    candidates[0].click();

    type("traceDevice", "new search");

    expect(node("traceDeviceId").value).toBe("");
    expect(node("traceDeviceOfferedSearch").value).toBe("");
    expect(node("traceDeviceOfferedOffset").value).toBe("");
    expect(node("traceDeviceSubmit").disabled).toBe(true);
  });
});

describe("trace Location picker", () => {
  beforeEach(() => {
    serve({
      candidates: [
        { id: 4, name: "DH4", parent: "1st Floor" },
        { id: 5, name: "T", parent: "" },
        { id: 6, name: "<img src=x onerror=alert(1)>", parent: "<b>Hall</b>" },
      ],
      shown: 3,
      total: 9,
    });
  });

  it("names the row that opened it", async () => {
    await open("openSecond", "traceLocation");

    expect(node("traceLocationLabel").textContent).toBe("Region >> DH5");
  });

  it("states the count, names a visible parent, and saves only the chosen Location", async () => {
    const candidates = await open("openFirst", "traceLocation");

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
    const candidates = await open("openFirst", "traceLocation");

    expect(candidates[2].querySelector("img, b")).toBeNull();
    expect(candidates[2].textContent).toBe("<img src=x onerror=alert(1)>In <b>Hall</b>");
  });

  it("clears the previous row's choice when another row opens the picker", async () => {
    const candidates = await open("openFirst", "traceLocation");
    candidates[0].click();

    await open("openSecond", "traceLocation");

    expect(node("traceLocationId").value).toBe("");
    expect(node("traceLocationSubmit").disabled).toBe(true);
    expect(node("traceLocationKey").value).toBe("region >> dh5");
  });
});
