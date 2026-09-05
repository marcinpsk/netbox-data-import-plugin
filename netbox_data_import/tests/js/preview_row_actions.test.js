/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Load the first-party browser asset in jsdom and drive its real delegated
 * submit handler, as the preview page does. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const controllerPath = resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/preview_row_actions.js",
);
const controllerSource = readFileSync(controllerPath, "utf8");

/* The controller delegates from `document`, so it is loaded once and then
 * reused against a fresh fixture for every test. */
let controllerLoaded = false;

function addPreviewFixture() {
  document.body.innerHTML = `
    <input type="hidden" name="csrfmiddlewaretoken" value="test-token">
    <input type="hidden" id="ndi-preview-revision" value="current-revision">
    <form class="ndi-field-review-form" action="/sync-device-field/">
      <button type="submit">Ignore</button>
    </form>
    <form id="mapping-form" class="ndi-deferred-preview-form" action="/quick-map/">
      <input type="hidden" name="source_model" value="Source Model">
      <button type="submit">Save mapping</button>
    </form>
    <div id="class-mapping-modal">
      <form id="class-mapping-form" class="ndi-deferred-preview-form" action="/quick-add-class-mapping/">
        <input type="hidden" name="source_class" value="Server">
        <button type="submit">Save mapping</button>
      </form>
    </div>
    <a href="/preview/" class="btn ndi-recalculate-preview" id="ndi-recalculate-preview">
      <i class="mdi mdi-refresh"></i> Recalculate Preview
    </a>
    <div id="ndi-preview-stale" hidden>
      <a href="/preview/" class="alert-link ndi-recalculate-preview">Recalculate Preview</a>
    </div>
    <button type="submit" id="ndi-run-import">Run import</button>
  `;
  if (!controllerLoaded) {
    window.eval(controllerSource);
    controllerLoaded = true;
  }
}

function submitReviewForm() {
  document
    .querySelector(".ndi-field-review-form")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  return new Promise((resolveTick) => setTimeout(resolveTick, 0));
}

function stubResponse(response) {
  const fetchMock = vi.fn(() => Promise.resolve(response));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  addPreviewFixture();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("preview row actions", () => {
  it("reports the HTTP status when the response is not JSON", async () => {
    stubResponse({
      ok: false,
      status: 500,
      json: () => Promise.reject(new SyntaxError("Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON")),
    });

    await submitReviewForm();

    const error = document.querySelector(".ndi-row-action-error");
    expect(error.textContent).toContain("HTTP 500");
    expect(error.textContent).not.toContain("Unexpected token");
  });

  it("keeps the server error message when the response is JSON", async () => {
    stubResponse({
      ok: false,
      status: 409,
      json: () => Promise.resolve({ ok: false, error: "The preview is stale." }),
    });

    await submitReviewForm();

    expect(document.querySelector(".ndi-row-action-error").textContent).toBe("The preview is stale.");
  });

  it("marks the button saved when the action succeeds", async () => {
    stubResponse({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true, preview_state: "recalculation_required", message: "Saved." }),
    });

    await submitReviewForm();

    expect(document.querySelector(".ndi-row-action-error")).toBeNull();
    expect(document.querySelector("button[type=submit]").textContent).toContain("Saved");
  });

  it("saves a quick mapping without leaving the preview", async () => {
    stubResponse({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true, preview_state: "recalculation_required", message: "Mapped." }),
    });
    const form = document.getElementById("mapping-form");

    const notCanceled = form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((resolveTick) => setTimeout(resolveTick, 0));

    expect(notCanceled).toBe(false);
    expect(form.querySelector("button").textContent).toContain("Saved");
    expect(document.getElementById("ndi-preview-stale").hidden).toBe(false);
    expect(document.getElementById("ndi-run-import").disabled).toBe(true);
  });

  it("resets a shared mapping modal for a second mapping", async () => {
    const fetchMock = stubResponse({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true, preview_state: "recalculation_required", message: "Mapped." }),
    });
    const form = document.getElementById("class-mapping-form");
    const button = form.querySelector("button");

    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((resolveTick) => setTimeout(resolveTick, 0));
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain("Saved");

    form.querySelector('[name="source_class"]').value = "Switch";
    document.getElementById("class-mapping-modal").dispatchEvent(new Event("show.bs.modal", { bubbles: true }));

    expect(button.disabled).toBe(false);
    expect(button.textContent).toBe("Save mapping");
    button.click();
    await new Promise((resolveTick) => setTimeout(resolveTick, 0));
    expect(button.textContent).toContain("Saved");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("reports that recalculation started and ignores a second press", () => {
    const link = document.getElementById("ndi-recalculate-preview");
    const staleLink = document.querySelector("#ndi-preview-stale .ndi-recalculate-preview");

    const first = link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));

    expect(first).toBe(true);
    expect(link.textContent).toContain("Recalculating");
    expect(link.querySelector(".mdi-spin")).not.toBeNull();
    expect(link.classList.contains("disabled")).toBe(true);
    expect(link.getAttribute("aria-busy")).toBe("true");
    // A screen reader reads `disabled` as styling, so the state needs its own attribute.
    expect(link.getAttribute("aria-disabled")).toBe("true");
    // Both links start the same recalculation, so the first press latches both.
    expect(staleLink.classList.contains("disabled")).toBe(true);

    expect(link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }))).toBe(false);
    expect(staleLink.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }))).toBe(false);
  });

  it("leaves the link alone when the press opens a second tab", () => {
    const link = document.getElementById("ndi-recalculate-preview");

    const opened = link.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true }),
    );

    expect(opened).toBe(true);
    expect(link.textContent).toContain("Recalculate Preview");
    expect(link.classList.contains("disabled")).toBe(false);
  });

  it("opens a second tab from a latched link", () => {
    const link = document.getElementById("ndi-recalculate-preview");

    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    const opened = link.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true }),
    );

    expect(opened).toBe(true);
  });

  it("opens a second tab from a latched link on a middle click", () => {
    const link = document.getElementById("ndi-recalculate-preview");

    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    const opened = link.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, button: 1 }),
    );

    expect(opened).toBe(true);
  });
});

describe("automatic recalculation", () => {
  it("latches the links and navigates to the recalculation the operator would press", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign, href: "http://localhost/preview/" });
    const link = document.getElementById("ndi-recalculate-preview");
    const staleLink = document.querySelector("#ndi-preview-stale .ndi-recalculate-preview");

    expect(window.ndiRecalculatePreview()).toBe(true);

    expect(assign).toHaveBeenCalledWith(link.href);
    expect(link.textContent).toContain("Recalculating");
    expect(link.getAttribute("aria-disabled")).toBe("true");
    expect(staleLink.classList.contains("disabled")).toBe(true);
  });

  it("reports that it started nothing when the page offers no recalculation", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign, href: "http://localhost/preview/" });
    document.querySelectorAll(".ndi-recalculate-preview").forEach((link) => link.remove());

    expect(window.ndiRecalculatePreview()).toBe(false);

    expect(assign).not.toHaveBeenCalled();
  });
});

describe("reporting a write the action already made", () => {
  it("keeps the named write on the page after the modal that made it closes", () => {
    window.ndiMarkPreviewStale("Contact 'Ada Lovelace' was created in NetBox.");

    const notice = document.getElementById("ndi-preview-stale");
    expect(notice.hidden).toBe(false);
    expect(notice.querySelector(".ndi-preview-stale-detail").textContent).toBe(
      "Contact 'Ada Lovelace' was created in NetBox.",
    );
  });

  it("says nothing extra when the action only recorded a decision", () => {
    window.ndiMarkPreviewStale("");

    const notice = document.getElementById("ndi-preview-stale");
    expect(notice.hidden).toBe(false);
    expect(notice.querySelector(".ndi-preview-stale-detail")).toBeNull();
  });

  it("replaces the previous write rather than stacking them up", () => {
    window.ndiMarkPreviewStale("First write.");
    window.ndiMarkPreviewStale("Second write.");

    const lines = document.querySelectorAll("#ndi-preview-stale .ndi-preview-stale-detail");
    expect(lines).toHaveLength(1);
    expect(lines[0].textContent).toBe("Second write.");
  });
});

describe("a recalculation that is already running", () => {
  it("starts one navigation when the helper is called twice", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign, href: "http://localhost/preview/" });

    expect(window.ndiRecalculatePreview()).toBe(true);
    expect(window.ndiRecalculatePreview()).toBe(true);

    // The links are latched after the first call, so the second must not navigate again.
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("starts one navigation when a press is followed by the helper", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { assign, href: "http://localhost/preview/" });
    document
      .getElementById("ndi-recalculate-preview")
      .dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));

    expect(window.ndiRecalculatePreview()).toBe(true);

    expect(assign).not.toHaveBeenCalled();
  });
});
