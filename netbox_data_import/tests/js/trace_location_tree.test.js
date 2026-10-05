/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const treeSource = readFileSync(resolve(
  process.cwd(),
  "netbox_data_import/static/netbox_data_import/js/trace_location_tree.js",
), "utf8");

function node(id) {
  return document.getElementById(id);
}

beforeEach(() => {
  document.body.innerHTML = `
    <ul>
      <li>
        <button id="open" type="button" data-trace-location-toggle aria-expanded="true"
                aria-controls="locationChildren0"><i id="openIcon"></i></button>
        <ul id="locationChildren0">
          <li>
            <button id="closed" type="button" data-trace-location-toggle aria-expanded="false"
                    aria-controls="locationChildren1"></button>
            <ul id="locationChildren1" hidden><li>T</li></ul>
          </li>
        </ul>
      </li>
      <li><button id="orphan" type="button" data-trace-location-toggle aria-expanded="false"
                  aria-controls="missing"></button></li>
    </ul>
  `;
  window.eval(treeSource);
});

afterEach(() => {
  document.body.replaceChildren();
});

describe("the Source Locations tree", () => {
  it("expands a collapsed node and collapses it again", () => {
    node("closed").click();

    expect(node("closed").getAttribute("aria-expanded")).toBe("true");
    expect(node("locationChildren1").hidden).toBe(false);

    node("closed").click();

    expect(node("closed").getAttribute("aria-expanded")).toBe("false");
    expect(node("locationChildren1").hidden).toBe(true);
  });

  it("collapses an expanded node from a click on its icon", () => {
    node("openIcon").click();

    expect(node("open").getAttribute("aria-expanded")).toBe("false");
    expect(node("locationChildren0").hidden).toBe(true);
  });

  it("toggles once per click after a second evaluation", () => {
    // An htmx boost evaluates the script again, so a second listener would undo the first.
    window.eval(treeSource);

    node("closed").click();

    expect(node("locationChildren1").hidden).toBe(false);
  });

  it("leaves a toggle whose list is gone unchanged", () => {
    node("orphan").click();

    expect(node("orphan").getAttribute("aria-expanded")).toBe("false");
  });
});
