/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The picker asks one question: which NetBox termination does this source port name?
 * It offers only the eligible candidates the server returns, and states how many of the
 * eligible set it is showing, so a capped page never reads as the whole answer. */
(function () {
  var modal = document.getElementById('traceTerminationPicker');
  var form = document.getElementById('traceTerminationForm');
  if (!modal || !form) return;

  var candidatesUrl = form.dataset.candidatesUrl;
  var list = document.getElementById('traceTerminationCandidates');
  var count = document.getElementById('traceTerminationCount');
  var search = document.getElementById('traceTerminationSearch');
  var title = document.getElementById('traceTerminationLabel');
  var error = document.getElementById('traceTerminationError');
  var fieldKey = document.getElementById('traceTerminationFieldKey');
  var objectType = document.getElementById('traceTerminationObjectType');
  var objectId = document.getElementById('traceTerminationObjectId');
  var submit = document.getElementById('traceTerminationSubmit');
  var offeredSearch = document.getElementById('traceTerminationOfferedSearch');
  var previewRevision = form.elements.namedItem('preview_revision');
  var kindLabels = {interface: 'dcim.interface', front_port: 'dcim.frontport', rear_port: 'dcim.rearport'};
  var activeKind = '';
  var pending = 0;

  function show(node, visible) {
    node.hidden = !visible;
  }

  function clearSelection() {
    objectId.value = '';
    objectType.value = '';
    // The offered search belongs to a selection, so it cannot outlive one.
    offeredSearch.value = '';
    submit.disabled = true;
  }

  function renderCandidates(payload, offered) {
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
        objectId.value = candidate.id;
        objectType.value = kindLabels[activeKind] || '';
        // The write rechecks the offer, so it needs the search that produced it.
        offeredSearch.value = item.dataset.offeredSearch;
        submit.disabled = !objectType.value;
      });
      list.appendChild(item);
    });
    count.textContent = (payload.shown || 0) + ' of ' + (payload.total || 0) + ' eligible';
    show(count, true);
  }

  function load() {
    var request = ++pending;
    var asked = search.value;
    var url = candidatesUrl + '?field_key=' + encodeURIComponent(fieldKey.value)
      + '&search=' + encodeURIComponent(search.value)
      + '&preview_revision=' + encodeURIComponent(previewRevision.value);
    fetch(url, {headers: {Accept: 'application/json'}, credentials: 'same-origin'})
      .then(function (response) {
        return response.json().then(function (payload) {
          return {ok: response.ok, payload: payload};
        });
      })
      .then(function (result) {
        // A slower earlier search must not overwrite the answer to a later one.
        if (request !== pending) return;
        if (!result.ok || !result.payload.ok) {
          list.replaceChildren();
          clearSelection();
          show(count, false);
          error.textContent = result.payload.error || 'The candidates could not be read.';
          show(error, true);
          return;
        }
        show(error, false);
        renderCandidates(result.payload, asked);
      })
      .catch(function () {
        if (request !== pending) return;
        list.replaceChildren();
        clearSelection();
        show(count, false);
        error.textContent = 'The candidates could not be read.';
        show(error, true);
      });
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-trace-picker]');
    if (!trigger) return;
    fieldKey.value = trigger.dataset.tracePicker;
    activeKind = trigger.dataset.traceKind || '';
    title.textContent = trigger.dataset.traceLabel || '';
    search.value = '';
    show(error, false);
    list.replaceChildren();
    show(count, false);
    clearSelection();
    load();
    new window.Modal(modal).show();
  });

  var searchTimer = null;
  search.addEventListener('input', function () {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(load, 200);
  });
})();
