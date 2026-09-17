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

  // A swap replaces every node this picker reads, so each one is read at the time it is used.
  function node(id) {
    return document.getElementById(id);
  }

  function show(target, visible) {
    if (target) target.hidden = !visible;
  }

  function clearSelection() {
    Array.prototype.forEach.call(node('traceTerminationCandidates').children, function (row) {
      row.classList.remove('active');
      row.setAttribute('aria-pressed', 'false');
    });
    node('traceTerminationObjectId').value = '';
    node('traceTerminationObjectType').value = '';
    // The offered search belongs to a selection, so it cannot outlive one.
    node('traceTerminationOfferedSearch').value = '';
    node('traceTerminationSubmit').disabled = true;
  }

  function renderCandidates(payload, offered) {
    var list = node('traceTerminationCandidates');
    clearSelection();
    list.replaceChildren();
    (payload.candidates || []).forEach(function (candidate) {
      var item = document.createElement('button');
      item.type = 'button';
      item.className = 'list-group-item list-group-item-action';
      item.setAttribute('aria-pressed', 'false');
      item.textContent = candidate.display || candidate.name;
      item.dataset.candidateId = candidate.id;
      // The offer belongs to the query that produced it, not to whatever the box says on click.
      item.dataset.offeredSearch = offered;
      item.addEventListener('click', function () {
        Array.prototype.forEach.call(list.children, function (row) {
          row.classList.remove('active');
          row.setAttribute('aria-pressed', 'false');
        });
        item.classList.add('active');
        item.setAttribute('aria-pressed', 'true');
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
    clearSelection();
    node('traceTerminationCandidates').replaceChildren();
    show(node('traceTerminationCount'), false);
    error.textContent = message;
    show(error, true);
  }

  var candidates = window.ndiPickerSearch({
    form: function () { return node('traceTerminationForm'); },
    search: function () { return node('traceTerminationSearch'); },
    url: function (form, asked) {
      return form.dataset.candidatesUrl + '?field_key=' + encodeURIComponent(node('traceTerminationFieldKey').value)
        + '&search=' + encodeURIComponent(asked)
        + '&preview_revision=' + encodeURIComponent(form.elements.namedItem('preview_revision').value);
    },
    onResult: function (payload, asked) {
      show(node('traceTerminationError'), false);
      renderCandidates(payload, asked);
    },
    onError: reportFailure
  });

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
    clearSelection();
    node('traceTerminationCandidates').replaceChildren();
    show(node('traceTerminationCount'), false);
    candidates.load();
    modal.addEventListener('hidden.bs.modal', function () { trigger.focus(); }, {once: true});
    ModalClass.getOrCreateInstance(modal).show(trigger);
  });

  document.addEventListener('keydown', function (event) {
    if (event.target.id === 'traceTerminationSearch' && event.key === 'Enter') event.preventDefault();
  });

  document.addEventListener('input', function (event) {
    if (event.target.id !== 'traceTerminationSearch') return;
    // The debounce leaves a window in which the old selection could still be submitted.
    clearSelection();
    candidates.reschedule(200);
  });
})();
