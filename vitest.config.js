/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The preview controller is a browser script. Run it in jsdom with real
 * selects and Tom Select instances, as the page does. */
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["netbox_data_import/tests/js/**/*.test.js"],
  },
});
