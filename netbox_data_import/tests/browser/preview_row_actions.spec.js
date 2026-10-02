/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { claim, claimForm, postedFields, script, servePage } from "./preview_page.js";

const previewTemplate = readFileSync(
  resolve(process.cwd(), "netbox_data_import/templates/netbox_data_import/import_preview.html"),
  "utf8",
);
const previewStyles = previewTemplate.match(/<style>([\s\S]*?)<\/style>/)[1];

const REPLANNED = {
  ok: true,
  row_number: 1,
  preview_state: "replanned",
  message: "Ignored the current u_position difference.",
  detail: "",
};

function previewPage(revision) {
  return `
    <input name="csrfmiddlewaretoken" value="token">
    ${claimForm(revision)}
    <output id="ndi-revision">${revision}</output>
    <!-- the device-type modal form: its own control named action shadows form.action -->
    <form id="ndi-device-type-form" class="ndi-deferred-preview-form" method="post"
          action="/plugins/data-import/quick-resolve-device-type/">
      <input type="hidden" name="action" value="map">
      <button type="submit">Save mapping</button>
    </form>
    <table><tbody>
      <tr id="row-1"><td>
        <button class="ndi-diff-toggle" data-diff-target="diff-1" aria-expanded="true">Fields differ</button>
      </td></tr>
      <!-- the field-difference row in its expanded state; the toggle has its own spec -->
      <tr id="diff-1" class="ndi-diff-row"><td>
        <form class="ndi-field-review-form" action="/plugins/data-import/ignore-field-difference/" method="post">
          <input name="row_number" value="1">
          <input name="target_field" value="u_position">
          <button type="submit">Ignore</button>
        </form>
        <button class="ndi-sync-placement-btn" data-row-id="1"
                data-action-url="/plugins/data-import/sync-placement/">Sync placement</button>
      </td></tr>
    </tbody></table>
    ${script("preview_claim.js")}
    ${script("preview_row_actions.js")}
  `;
}

/* Answers each POST to `path` with the next of `answers`, and records what it was sent. */
async function answerPosts(page, path, answers) {
  const posted = [];
  await page.route(`**${path}`, async (route) => {
    posted.push(await postedFields(route.request()));
    const answer = answers[Math.min(posted.length - 1, answers.length - 1)];
    if (answer.delay) await new Promise((resolveDelay) => setTimeout(resolveDelay, answer.delay));
    await route.fulfill({ status: answer.status || 200, json: answer.body });
  });
  return posted;
}

test("Ignore posts the page claim and reloads the replanned preview", async ({ page }) => {
  const posted = await answerPosts(page, "/ignore-field-difference/", [{ body: REPLANNED, delay: 100 }]);
  const loads = await servePage(page, previewPage);

  const button = page.locator(".ndi-field-review-form button");
  await button.click();
  await expect(button).toBeDisabled();
  await expect(button).toContainText("Updating");

  await expect(page.locator("#ndi-revision")).toHaveText("5");
  expect(loads()).toBe(2);
  expect(posted).toEqual([{ row_number: "1", target_field: "u_position", ...claim() }]);
});

test("a deferred form posts to its action attribute, not to a control that shadows it", async ({ page }) => {
  let requestedUrl = "";
  await page.route("**/quick-resolve-device-type/", async (route) => {
    requestedUrl = route.request().url();
    await route.fulfill({ json: { ...REPLANNED, message: "Mapped the device type." } });
  });
  await servePage(page, previewPage);

  await page.locator("#ndi-device-type-form button").click();

  await expect(page.locator("#ndi-revision")).toHaveText("5");
  expect(requestedUrl).toBe("http://preview.test/plugins/data-import/quick-resolve-device-type/");
});

test("placement sync posts the page claim and reloads the replanned preview", async ({ page }) => {
  const posted = await answerPosts(page, "/sync-placement/", [{ body: { ...REPLANNED, message: "Placed." } }]);
  await servePage(page, previewPage);

  await page.locator(".ndi-sync-placement-btn").click();

  await expect(page.locator("#ndi-revision")).toHaveText("5");
  expect(posted).toEqual([{ row_number: "1", ...claim() }]);
});

test("a stale claim refusal shows in place, does not reload, and clears on retry", async ({ page }) => {
  const posted = await answerPosts(page, "/ignore-field-difference/", [
    { status: 409, body: { ok: false, error: "A newer preview replaced this one.", code: "preview_stale" } },
    { body: REPLANNED },
  ]);
  const loads = await servePage(page, previewPage);

  const button = page.locator(".ndi-field-review-form button");
  await button.click();

  await expect(button).toBeEnabled();
  await expect(button).toHaveClass(/btn-danger/);
  await expect(page.locator(".ndi-row-action-error")).toHaveText("A newer preview replaced this one.");
  expect(loads()).toBe(1);

  await button.click();

  await expect(page.locator("#ndi-revision")).toHaveText("5");
  expect(posted).toHaveLength(2);
  await expect(page.locator(".ndi-row-action-error")).toHaveCount(0);
});

test("sequential row actions each post the claim of the page they were taken on", async ({ page }) => {
  const posted = await answerPosts(page, "/ignore-field-difference/", [{ body: REPLANNED }]);
  await servePage(page, previewPage);

  await page.locator(".ndi-field-review-form button").click();
  await expect(page.locator("#ndi-revision")).toHaveText("5");
  await page.locator(".ndi-field-review-form button").click();
  await expect(page.locator("#ndi-revision")).toHaveText("6");

  expect(posted.map((fields) => fields.preview_revision)).toEqual(["4", "5"]);
});

test("ignored field badge keeps readable contrast in both themes", async ({ page }) => {
  await page.setContent(`
    <style>
      :root { --tblr-dark: #1f2937; }
      .text-muted { color: #6b7280; }
      ${previewStyles}
    </style>
    <div class="text-muted">
      <button class="badge ndi-badge-ignored ndi-diff-toggle">1 field(s) ignored</button>
    </div>
  `);

  async function contrastRatio(theme) {
    await page.locator("html").evaluate((html, selectedTheme) => {
      html.setAttribute("data-bs-theme", selectedTheme);
    }, theme);
    return page.locator("button").evaluate((button) => {
      function rgb(value) {
        return value.match(/[\d.]+/g).slice(0, 3).map(Number);
      }
      function luminance(color) {
        const channels = color.map((channel) => {
          const normalized = channel / 255;
          return normalized <= 0.04045
            ? normalized / 12.92
            : ((normalized + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
      }
      const style = getComputedStyle(button);
      const foreground = luminance(rgb(style.color));
      const background = luminance(rgb(style.backgroundColor));
      return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
    });
  }

  expect(await contrastRatio("light")).toBeGreaterThanOrEqual(4.5);
  expect(await contrastRatio("dark")).toBeGreaterThanOrEqual(4.5);
});
