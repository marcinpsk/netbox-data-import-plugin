/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Filter the Source Trace list by disposition, and remember the choice in this browser for every preview.
 * No pill pressed shows every trace, and the selected trace always stays in the list. */
(function () {
  var KEY = 'ndi.traceWorkspace.dispositionFilter';

  // The page keeps the viewer's last choice only when storage refused to write it.
  function remembered() {
    if (window.ndiTraceListFilterUnsaved) return window.ndiTraceListFilterUnsaved;
    try {
      var value = JSON.parse(window.localStorage.getItem(KEY));
      return Array.isArray(value) ? value : [];
    } catch (_) {
      return [];
    }
  }

  function remember(chosen) {
    try {
      window.localStorage.setItem(KEY, JSON.stringify(chosen));
      delete window.ndiTraceListFilterUnsaved;
    } catch (_) {
      window.ndiTraceListFilterUnsaved = chosen;
    }
  }

  function pills(card) {
    return Array.prototype.slice.call(card.querySelectorAll('button[data-trace-filter]'));
  }

  // A stored value that names no pill on this page is ignored, so a stale or foreign value hides nothing.
  function apply(card, chosen) {
    var offered = pills(card);
    var selection = chosen.filter(function (value) {
      return offered.some(function (pill) { return pill.dataset.traceFilter === value; });
    });
    offered.forEach(function (pill) {
      pill.setAttribute('aria-pressed', String(selection.indexOf(pill.dataset.traceFilter) !== -1));
    });
    var shown = 0;
    Array.prototype.forEach.call(card.querySelectorAll('[data-disposition]'), function (item) {
      var visible = !selection.length || item.classList.contains('active')
        || selection.indexOf(item.dataset.disposition) !== -1;
      item.hidden = !visible;
      if (visible) shown += 1;
    });
    var empty = card.querySelector('[data-trace-filter-empty]');
    if (empty) empty.hidden = shown > 0;
  }

  function applyAll(chosen) {
    Array.prototype.forEach.call(document.querySelectorAll('[data-trace-list-filter]'), function (card) {
      apply(card, chosen);
    });
  }

  applyAll(remembered());
  // An htmx swap evaluates the script again, and one set of document listeners is enough.
  if (window.ndiTraceListFilter) return;
  window.ndiTraceListFilter = true;

  document.addEventListener('click', function (event) {
    var pill = event.target.closest && event.target.closest('button[data-trace-filter]');
    if (!pill) return;
    var card = pill.closest('[data-trace-list-filter]');
    if (!card) return;
    var chosen = pills(card).filter(function (item) {
      return (item === pill) !== (item.getAttribute('aria-pressed') === 'true');
    }).map(function (item) { return item.dataset.traceFilter; });
    remember(chosen);
    applyAll(chosen);
  });

  // An action swaps in a new list, rendered unfiltered, so the filter applies again before the browser paints it.
  ['htmx:afterSwap', 'htmx:afterSettle', 'htmx:load'].forEach(function (name) {
    document.addEventListener(name, function () { applyAll(remembered()); });
  });
}());
