/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The one claim helper every preview script posts and reads through. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeAll, describe, expect, it } from "vitest";

const claimSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js/preview_claim.js"),
  "utf8",
);

const CLAIM = [
  ["preview_token", "token-1"],
  ["preview_revision", "4"],
  ["preview_document", "11"],
  ["preview_profile", "3"],
];

function renderClaim() {
  const inputs = CLAIM.map(([name, value]) => `<input type="hidden" name="${name}" value="${value}">`).join("");
  document.body.innerHTML = `<form id="ndi-preview-claim" hidden>${inputs}</form>`;
}

beforeAll(() => {
  window.eval(claimSource);
});

describe("the page claim helper", () => {
  it("copies every claim field into the target it is given", () => {
    renderClaim();
    const form = new FormData();

    expect(window.ndiPreviewClaim(form)).toBe(form);

    expect([...form]).toEqual(CLAIM);
  });

  it("overwrites a stale claim field in the body it is given", () => {
    renderClaim();
    const body = new URLSearchParams({ name: "Role", preview_revision: "1" });

    window.ndiPreviewClaim(body);

    expect([...body]).toEqual([
      ["name", "Role"],
      ["preview_revision", "4"],
      ...CLAIM.filter(([name]) => name !== "preview_revision"),
    ]);
  });

  it("refuses a page that holds no claim", () => {
    document.body.innerHTML = "";

    expect(() => window.ndiPreviewClaim(new URLSearchParams())).toThrow(
      "The page holds no preview claim. Reload the page.",
    );
  });
});
