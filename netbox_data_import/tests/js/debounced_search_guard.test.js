/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* A debounced search must invalidate the request in flight, not only the one it replaces. */

import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SCRIPT_DIRECTORY = resolve(process.cwd(), "netbox_data_import/static/netbox_data_import/js");

function inputListenerBody(source) {
  const start = source.indexOf("addEventListener('input'");
  if (start < 0) return null;
  const end = source.indexOf("\n  });", start);
  return end < 0 ? null : source.slice(start, end);
}

const guarded = readdirSync(SCRIPT_DIRECTORY)
  .filter(name => name.endsWith(".js"))
  .map(name => ({ name, source: readFileSync(resolve(SCRIPT_DIRECTORY, name), "utf8") }))
  .filter(file => file.source.includes("request !== pending"));

describe("debounced search freshness", () => {
  it("covers every script that ranks answers by a pending counter", () => {
    expect(guarded.map(file => file.name).sort()).toEqual(
      ["trace_device_picker.js", "trace_termination_picker.js"],
    );
  });

  it.each(guarded)("$name invalidates the request in flight when the search changes", ({ source }) => {
    const body = inputListenerBody(source);

    expect(body).not.toBeNull();
    expect(body).toMatch(/pending\s*\+=\s*1|\+\+pending/);
  });
});
