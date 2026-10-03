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
const claimSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/preview_claim.js"),
  "utf8",
);

/* The controller delegates from `document`, so it is loaded once and then
 * reused against a fresh fixture for every test. */
let controllerLoaded = false;

const CLAIM = [
  ["preview_token", "token-1"],
  ["preview_revision", "4"],
  ["preview_document", "11"],
  ["preview_profile", "3"],
];

function addPreviewFixture() {
  const claim = CLAIM.map(([name, value]) => `<input type="hidden" name="${name}" value="${value}">`).join("");
  document.body.innerHTML = `
    <input type="hidden" name="csrfmiddlewaretoken" value="test-token">
    <form id="ndi-preview-claim" hidden>${claim}</form>
    <form class="ndi-field-review-form" action="/sync-device-field/">
      <button type="submit">Ignore</button>
    </form>
    <form id="mapping-form" class="ndi-deferred-preview-form" action="/quick-map/">
      <input type="hidden" name="source_model" value="Source Model">
      <button type="submit">Save mapping</button>
    </form>
    <button class="ndi-sync-placement-btn" data-row-id="5" data-action-url="/sync-placement/">Sync placement</button>
  `;
  if (!controllerLoaded) {
    window.eval(claimSource);
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

function replanned(extra = {}) {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve({ ok: true, preview_state: "replanned", message: "Saved.", ...extra }),
  };
}

let reload;

beforeEach(() => {
  addPreviewFixture();
  reload = vi.fn();
  vi.stubGlobal("location", { reload });
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.ndiRememberPreviewView;
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
    expect(reload).not.toHaveBeenCalled();
  });

  it("shows a stale claim refusal in place and does not reload", async () => {
    stubResponse({
      ok: false,
      status: 409,
      json: () => Promise.resolve({ ok: false, error: "The preview is stale.", code: "preview_stale" }),
    });

    await submitReviewForm();

    expect(document.querySelector(".ndi-row-action-error")?.textContent).toBe("The preview is stale.");
    expect(document.querySelector(".ndi-field-review-form button").disabled).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });

  it("posts the page claim and reloads the replanned preview", async () => {
    const fetchMock = stubResponse(replanned());

    await submitReviewForm();

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("/sync-device-field/");
    expect(options.headers).toEqual({ Accept: "application/json", "X-CSRFToken": "test-token" });
    expect([...options.body]).toEqual(CLAIM);
    expect(document.querySelector(".ndi-row-action-error")).toBeNull();
    expect(reload).toHaveBeenCalledOnce();
  });

  it("refuses a success envelope that does not report a replanned preview", async () => {
    stubResponse({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true, preview_state: "recalculation_required", message: "Saved." }),
    });

    await submitReviewForm();

    expect(document.querySelector(".ndi-row-action-error")?.textContent).toBe(
      "The preview action returned an invalid state.",
    );
    expect(reload).not.toHaveBeenCalled();
  });

  it("saves a quick mapping through the claim and reloads instead of following the form", async () => {
    const fetchMock = stubResponse(replanned({ message: "Mapped." }));
    const form = document.getElementById("mapping-form");

    const notCanceled = form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await new Promise((resolveTick) => setTimeout(resolveTick, 0));

    expect(notCanceled).toBe(false);
    expect([...fetchMock.mock.calls[0][1].body]).toEqual([["source_model", "Source Model"], ...CLAIM]);
    expect(reload).toHaveBeenCalledOnce();
  });

  it("reloads once when two actions succeed before the page leaves", async () => {
    stubResponse(replanned());

    await submitReviewForm();
    document.querySelector(".ndi-sync-placement-btn").click();
    await new Promise((resolveTick) => setTimeout(resolveTick, 0));

    expect(fetch).toHaveBeenCalledTimes(2);
    expect(reload).toHaveBeenCalledOnce();
  });

  it("remembers the view before it reloads", async () => {
    stubResponse(replanned());
    const order = [];
    reload.mockImplementation(() => order.push("reload"));
    window.ndiRememberPreviewView = () => order.push("remember");

    await submitReviewForm();

    expect(order).toEqual(["remember", "reload"]);
  });

  it("posts the claim from a placement button and alerts on a refusal", async () => {
    const alert = vi.fn();
    vi.stubGlobal("alert", alert);
    const fetchMock = stubResponse({
      ok: false,
      status: 409,
      json: () => Promise.resolve({ ok: false, error: "The preview is stale.", code: "preview_stale" }),
    });

    document.querySelector(".ndi-sync-placement-btn").click();
    await new Promise((resolveTick) => setTimeout(resolveTick, 0));

    expect([...fetchMock.mock.calls[0][1].body]).toEqual([["row_number", "5"], ...CLAIM]);
    expect(alert).toHaveBeenCalledWith("Placement sync failed: The preview is stale.");
    expect(reload).not.toHaveBeenCalled();
  });

  it("refuses to post without a page claim", async () => {
    const fetchMock = stubResponse(replanned());
    document.getElementById("ndi-preview-claim").remove();

    await submitReviewForm();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(document.querySelector(".ndi-row-action-error")?.textContent).toBe(
      "The page holds no preview claim. Reload the page.",
    );
    expect(reload).not.toHaveBeenCalled();
  });
});
