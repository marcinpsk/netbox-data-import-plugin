/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The picker asks one question: which NetBox termination does this source port name?
 * It offers only the eligible candidates the server returns, and states how many of the
 * eligible set it is showing, so a capped page never reads as the whole answer. */
(function () {
  // The script ships inside the swapped content, so an htmx boost evaluates it again on every
  // navigation. Document listeners outlive the swap, so a second evaluation would double them.
  if (window.ndiTraceTerminationPicker) return;
  window.ndiTraceTerminationPicker = true;

  var kindLabels = {interface: 'dcim.interface', front_port: 'dcim.frontport', rear_port: 'dcim.rearport'};
  var activeKind = '';
  var pending = 0;
  var searchTimer = null;

  // A swap replaces every node this picker reads, so each one is read at the time it is used.
  function node(id) {
    return document.getElementById(id);
  }

  function show(target, visible) {
    if (target) target.hidden = !visible;
  }

  function clearSelection() {
    node('traceTerminationObjectId').value = '';
    node('traceTerminationObjectType').value = '';
    // The offered search belongs to a selection, so it cannot outlive one.
    node('traceTerminationOfferedSearch').value = '';
    node('traceTerminationSubmit').disabled = true;
  }

  function renderCandidates(payload, offered) {
    var list = node('traceTerminationCandidates');
    list.replaceChildren();
    clearSelection();
    (payload.candidates || []).forEach(function (candidate) {
      var item = document.createElement('button');
      item.type = 'button';
      item.className = 'list-group-item list-group-item-action';
      item.textContent = candidate.display || candidate.name;
      item.dataset.candidateId = candidate.id;
      // The offer belongs to the query that produced it, not to whatever the box says on click.
      item.dataset.offeredSearch = offered;
      item.addEventListener('click', function () {
        Array.prototype.forEach.call(list.children, function (row) {
          row.classList.remove('active');
        });
        item.classList.add('active');
        var objectType = node('traceTerminationObjectType');
        node('traceTerminationObjectId').value = candidate.id;
        objectType.value = kindLabels[activeKind] || '';
        // The write rechecks the offer, so it needs the search that produced it.
        node('traceTerminationOfferedSearch').value = item.dataset.offeredSearch;
        node('traceTerminationSubmit').disabled = !objectType.value;
      });
      list.appendChild(item);
    });
    var count = node('traceTerminationCount');
    count.textContent = (payload.shown || 0) + ' of ' + (payload.total || 0) + ' eligible';
    show(count, true);
  }

  function reportFailure(message) {
    var error = node('traceTerminationError');
    node('traceTerminationCandidates').replaceChildren();
    clearSelection();
    show(node('traceTerminationCount'), false);
    error.textContent = message;
    show(error, true);
  }

  function load() {
    var form = node('traceTerminationForm');
    var search = node('traceTerminationSearch');
    // A boost can land on a page with no picker while a debounce is still pending.
    if (!form || !search) return;
    var request = ++pending;
    var asked = search.value;
    var url = form.dataset.candidatesUrl + '?field_key=' + encodeURIComponent(node('traceTerminationFieldKey').value)
      + '&search=' + encodeURIComponent(search.value)
      + '&preview_revision=' + encodeURIComponent(form.elements.namedItem('preview_revision').value);
    fetch(url, {headers: {Accept: 'application/json'}, credentials: 'same-origin'})
      .then(function (response) {
        return response.json().then(function (payload) {
          return {ok: response.ok, payload: payload};
        });
      })
      .then(function (result) {
        // A slower earlier search must not overwrite the answer to a later one, and an answer to
        // the page a boost replaced must not be shown on the page that replaced it.
        if (request !== pending || node('traceTerminationForm') !== form) return;
        if (!result.ok || !result.payload.ok) {
          reportFailure(result.payload.error || 'The candidates could not be read.');
          return;
        }
        show(node('traceTerminationError'), false);
        renderCandidates(result.payload, asked);
      })
      .catch(function () {
        if (request !== pending || node('traceTerminationForm') !== form) return;
        reportFailure('The candidates could not be read.');
      });
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-trace-picker]');
    if (!trigger) return;
    var modal = node('traceTerminationPicker');
    if (!modal || !node('traceTerminationForm')) return;
    // NetBox/Tabler exposes Bootstrap as global `Modal`, not `bootstrap.Modal`.
    var ModalClass = (typeof bootstrap !== 'undefined' && bootstrap.Modal) || window.Modal;
    if (!ModalClass) return;
    node('traceTerminationFieldKey').value = trigger.dataset.tracePicker;
    activeKind = trigger.dataset.traceKind || '';
    node('traceTerminationLabel').textContent = trigger.dataset.traceLabel || '';
    node('traceTerminationSearch').value = '';
    show(node('traceTerminationError'), false);
    node('traceTerminationCandidates').replaceChildren();
    show(node('traceTerminationCount'), false);
    clearSelection();
    load();
    modal.addEventListener('hidden.bs.modal', function () { trigger.focus(); }, {once: true});
    ModalClass.getOrCreateInstance(modal).show(trigger);
  });

  document.addEventListener('input', function (event) {
    if (event.target.id !== 'traceTerminationSearch') return;
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(load, 200);
  });
})();
