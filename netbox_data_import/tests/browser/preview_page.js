/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* A routed preview page that the scripts can reload, with the Preview Claim the server renders.
 * Each load renders the next revision, as a successful command advances it (ADR 0004). */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export const PREVIEW_URL = "http://preview.test/plugins/data-import/import/preview/";
export const FIRST_REVISION = 4;

export function claim(revision = FIRST_REVISION) {
  return {
    preview_token: "token-1",
    preview_revision: String(revision),
    preview_document: "11",
    preview_profile: "3",
  };
}

export function claimInputs(revision = FIRST_REVISION) {
  return Object.entries(claim(revision))
    .map(([name, value]) => `<input type="hidden" name="${name}" value="${value}">`)
    .join("");
}

export function claimForm(revision = FIRST_REVISION) {
  return `<form id="ndi-preview-claim" hidden>${claimInputs(revision)}</form>`;
}

export function scriptSource(name) {
  return readFileSync(resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js", name), "utf8");
}

export function script(name) {
  return `<script>${scriptSource(name)}</script>`;
}

/* Serves `render(revision)` at `url` for every load and returns the number of loads so far. */
export async function servePage(page, render, url = PREVIEW_URL) {
  let loads = 0;
  await page.route(url, (route) => {
    const revision = FIRST_REVISION + loads;
    loads += 1;
    return route.fulfill({ contentType: "text/html; charset=utf-8", body: render(revision) });
  });
  await page.goto(url);
  return () => loads;
}

/* The fields of a posted form, urlencoded or multipart, read by the platform's own parser. */
export async function postedFields(request) {
  const body = new Response(request.postDataBuffer(), {
    headers: { "content-type": request.headers()["content-type"] },
  });
  return Object.fromEntries(await body.formData());
}
