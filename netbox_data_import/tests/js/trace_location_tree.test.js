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

function row(id, depth, parent, { expanded = null, hidden = false } = {}) {
  const toggle = expanded === null
    ? ""
    : `<button id="${id}Toggle" type="button" data-trace-location-toggle aria-expanded="${expanded}"><i id="${id}Icon"></i></button>`;
  const parentAttribute = parent ? ` data-parent="${parent}"` : "";
  return `<li id="${id}" data-depth="${depth}"${parentAttribute}${hidden ? " hidden" : ""}>${toggle}</li>`;
}

function shown() {
  return Array.from(document.querySelectorAll("li")).filter((item) => !item.hidden).map((item) => item.id);
}

// Region (open) > DH4 (closed) > T (closed) > 01; Region > DH5; Annex is a second top node.
beforeEach(() => {
  document.body.innerHTML = `<ul>${[
    row("region", 0, null, { expanded: true }),
    row("hall", 1, "region", { expanded: false }),
    row("rack", 2, "hall", { expanded: false, hidden: true }),
    row("port", 3, "rack", { hidden: true }),
    row("otherHall", 1, "region"),
    row("annex", 0, null),
  ].join("")}</ul>`;
  window.eval(treeSource);
});

afterEach(() => {
  document.body.replaceChildren();
});

describe("the Source Locations tree", () => {
  it("expands a node to its children and keeps a collapsed child's subtree hidden", () => {
    node("hallToggle").click();

    expect(node("hallToggle").getAttribute("aria-expanded")).toBe("true");
    expect(shown()).toEqual(["region", "hall", "rack", "otherHall", "annex"]);

    node("rackToggle").click();

    expect(shown()).toEqual(["region", "hall", "rack", "port", "otherHall", "annex"]);
  });

  it("collapses a node with its whole subtree and restores it as it was", () => {
    node("hallToggle").click();
    node("rackToggle").click();

    node("regionIcon").click();

    expect(node("regionToggle").getAttribute("aria-expanded")).toBe("false");
    expect(shown()).toEqual(["region", "annex"]);

    node("regionToggle").click();

    expect(shown()).toEqual(["region", "hall", "rack", "port", "otherHall", "annex"]);
  });

  it("toggles once per click after a second evaluation", () => {
    // An htmx boost evaluates the script again, so a second listener would undo the first.
    window.eval(treeSource);

    node("hallToggle").click();

    expect(node("rack").hidden).toBe(false);
  });

  it("ignores a click outside a toggle", () => {
    node("port").click();
    node("annex").click();

    expect(shown()).toEqual(["region", "hall", "otherHall", "annex"]);
  });
});
