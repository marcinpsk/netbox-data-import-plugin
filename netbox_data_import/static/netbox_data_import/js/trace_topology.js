/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Remember in this browser whether the trace topology group is open. The server renders it closed,
 * so the page works the same when the browser keeps no storage. */
(function () {
  var KEY = 'ndi.traceWorkspace.topologyOpen';

  // The page keeps the viewer's last choice, so a storage write that failed cannot undo it.
  function remembered() {
    if (window.ndiTraceTopologyOpen) return window.ndiTraceTopologyOpen;
    try {
      return window.localStorage.getItem(KEY);
    } catch (_) {
      return null;
    }
  }

  function apply(root) {
    var value = remembered();
    if (value !== 'true' && value !== 'false') return;
    var groups = root.matches && root.matches('details[data-trace-topology]')
      ? [root] : root.querySelectorAll('details[data-trace-topology]');
    Array.prototype.forEach.call(groups, function (group) {
      group.open = value === 'true';
    });
  }

  apply(document);
  // An htmx boost evaluates the script again on every navigation, and one document listener is enough.
  if (window.ndiTraceTopology) return;
  window.ndiTraceTopology = true;

  // The toggle event does not bubble, so the document listens in the capture phase.
  document.addEventListener('toggle', function (event) {
    if (!event.target.matches || !event.target.matches('details[data-trace-topology]')) return;
    window.ndiTraceTopologyOpen = String(event.target.open);
    try {
      window.localStorage.setItem(KEY, String(event.target.open));
    } catch (_) {
      // Without storage the group keeps the server default on the next page.
    }
  }, true);

  // An accepted proposal swaps in a new group, rendered closed; a card refresh brings none and keeps the open one.
  document.addEventListener('htmx:load', function (event) {
    apply(event.target);
  });
}());
