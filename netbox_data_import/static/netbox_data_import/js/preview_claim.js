/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The Preview Claim (ADR 0004) names the preview a page shows. Every command and every read that
 * answers for that preview copies it from the page's `#ndi-preview-claim` form through this helper.
 * The form renders the claim include, so the template alone names the fields. */
(function () {
  window.ndiPreviewClaim = function (target) {
    var claim = document.getElementById('ndi-preview-claim');
    if (!claim) throw new Error('The page holds no preview claim. Reload the page.');
    new FormData(claim).forEach(function (value, name) { target.set(name, value); });
    return target;
  };
}());
