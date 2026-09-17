/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The debounced candidate search has one owner, and scheduling it retires the request in flight.
 * Both pickers once held their own copy, and both let an answer to a superseded query render. */

import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SCRIPT_DIRECTORY = resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js");
const OWNER = "trace_picker_search.js";

const scripts = readdirSync(SCRIPT_DIRECTORY)
  .filter(name => name.endsWith(".js"))
  .map(name => ({ name, source: readFileSync(resolve(SCRIPT_DIRECTORY, name), "utf8") }));

function owner() {
  return scripts.find(file => file.name === OWNER).source;
}

describe("debounced search freshness", () => {
  it("keeps the request-freshness mechanism in one file", () => {
    // The counter, not the word: other scripts use `pending` for a proposal's status.
    const holders = scripts.filter(file => /\+\+pending|pending\s*\+=\s*1/.test(file.source)).map(file => file.name);

    expect(holders).toEqual([OWNER]);
  });

  it("retires the request in flight whenever a search is rescheduled", () => {
    const reschedule = owner().slice(owner().indexOf("reschedule:"));

    expect(reschedule).toMatch(/pending\s*\+=\s*1[\s\S]*setTimeout/);
  });

  it("gives no caller a way to schedule a search without retiring the one in flight", () => {
    const callers = scripts.filter(file => file.name !== OWNER);

    for (const file of callers) {
      expect(file.source, `${file.name} schedules its own search`).not.toMatch(/setTimeout\(\s*load/);
    }
  });
});
