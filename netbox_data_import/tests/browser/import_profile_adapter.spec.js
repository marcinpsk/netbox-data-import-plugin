/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const templateSource = readFileSync(
  resolve(process.cwd(), "netbox_data_import/templates/netbox_data_import/importprofile_edit.html"),
  "utf8",
);
const controllerSource = templateSource.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const fixture = `
  <select id="id_source_adapter">
    <option value="flat_workbook">Flat workbook</option>
    <option value="trace_workbook">Trace workbook</option>
  </select>
`;
const formFixture = `
  <form>
    <input name="return_url" value="/plugins/data-import/import-profiles/">
    <input name="name" value="Initial profile">
    <textarea name="description">Initial description</textarea>
    <select name="tags" multiple>
      <option value="1" selected>One</option>
      <option value="2">Two</option>
      <option value="3">Three</option>
    </select>
    <select id="id_source_adapter" name="source_adapter">
      <option value="flat_workbook">Flat workbook</option>
      <option value="trace_workbook">Trace workbook</option>
    </select>
  </form>
`;

test("adapter reload keeps stable profile values and drops adapter settings", async ({ page }) => {
  await page.route("http://profile.test/**", async (route) => {
    await route.fulfill({ contentType: "text/html", body: fixture });
  });
  await page.goto(
    "http://profile.test/add/?return_url=%2Fplugins%2Fdata-import%2Fimport-profiles%2F" +
      "&name=Trace+Profile&description=Keep+this&tags=1&tags=2&sheet_name=Old&update_existing=on",
  );
  await page.addScriptTag({ content: controllerSource });

  await Promise.all([
    page.waitForURL((url) => url.searchParams.get("source_adapter") === "trace_workbook"),
    page.locator("#id_source_adapter").selectOption("trace_workbook"),
  ]);

  const url = new URL(page.url());
  expect(url.searchParams.get("return_url")).toBe("/plugins/data-import/import-profiles/");
  expect(url.searchParams.get("name")).toBe("Trace Profile");
  expect(url.searchParams.get("description")).toBe("Keep this");
  expect(url.searchParams.getAll("tags")).toEqual(["1", "2"]);
  expect(url.searchParams.has("sheet_name")).toBe(false);
  expect(url.searchParams.has("update_existing")).toBe(false);
});

test("adapter reload keeps values entered in the add form", async ({ page }) => {
  await page.route("http://profile.test/**", async (route) => {
    await route.fulfill({ contentType: "text/html", body: formFixture });
  });
  await page.goto(
    "http://profile.test/add/?return_url=%2Fold%2F&name=Old+name&description=Old+description&tags=1",
  );
  await page.addScriptTag({ content: controllerSource });
  await page.locator('[name="name"]').fill("Typed profile");
  await page.locator('[name="description"]').fill("Typed description");
  await page.locator('[name="tags"]').selectOption(["2", "3"]);

  await Promise.all([
    page.waitForURL((url) => url.searchParams.get("source_adapter") === "trace_workbook"),
    page.locator("#id_source_adapter").selectOption("trace_workbook"),
  ]);

  const url = new URL(page.url());
  expect(url.searchParams.get("return_url")).toBe("/plugins/data-import/import-profiles/");
  expect(url.searchParams.get("name")).toBe("Typed profile");
  expect(url.searchParams.get("description")).toBe("Typed description");
  expect(url.searchParams.getAll("tags")).toEqual(["2", "3"]);
});
