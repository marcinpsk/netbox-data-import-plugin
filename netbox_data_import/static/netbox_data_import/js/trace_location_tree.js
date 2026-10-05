/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Expand and collapse the nodes of the Source Locations tree. The server sets each node's default. */
(function () {
  // An htmx boost evaluates the script again on every navigation, and one document listener is enough.
  if (window.ndiTraceLocationTree) return;
  window.ndiTraceLocationTree = true;

  document.addEventListener('click', function (event) {
    var toggle = event.target.closest && event.target.closest('[data-trace-location-toggle]');
    if (!toggle) return;
    var children = document.getElementById(toggle.getAttribute('aria-controls'));
    if (!children) return;
    var expanded = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(expanded));
    children.hidden = !expanded;
  });
})();
