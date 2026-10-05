/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Expand and collapse the nodes of the Source Locations tree. The server sets each node's default. */
(function () {
  // An htmx boost evaluates the script again on every navigation, and one document listener is enough.
  if (window.ndiTraceLocationTree) return;
  window.ndiTraceLocationTree = true;

  function expanded(row) {
    var toggle = row.querySelector('[data-trace-location-toggle]');
    return Boolean(toggle) && toggle.getAttribute('aria-expanded') === 'true';
  }

  document.addEventListener('click', function (event) {
    var toggle = event.target.closest && event.target.closest('[data-trace-location-toggle]');
    var row = toggle && toggle.closest('[data-depth]');
    if (!row) return;
    toggle.setAttribute('aria-expanded', String(toggle.getAttribute('aria-expanded') !== 'true'));
    // The rows are flat in page order, so the subtree is every next row that is deeper than this one.
    var depth = Number(row.dataset.depth);
    var shown = {};
    shown[row.id] = !row.hidden;
    for (var next = row.nextElementSibling; next && Number(next.dataset.depth) > depth; next = next.nextElementSibling) {
      var parent = document.getElementById(next.dataset.parent);
      next.hidden = !(parent && shown[parent.id] && expanded(parent));
      shown[next.id] = !next.hidden;
    }
  });
})();
