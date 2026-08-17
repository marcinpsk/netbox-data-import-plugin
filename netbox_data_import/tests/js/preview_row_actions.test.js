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
  vi.stubGlobal("fetch", () => Promise.resolve(response));
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
});
