/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Map one source Location path to a server-offered NetBox Location, through one dialog every row shares. */
(function () {
  if (window.ndiTraceLocationPicker) return;
  window.ndiTraceLocationPicker = true;

  function node(id) {
    return document.getElementById(id);
  }

  function show(target, visible) {
    if (target) target.hidden = !visible;
  }

  function clearSelection() {
    var list = node('traceLocationCandidates');
    if (list) {
      Array.prototype.forEach.call(list.children, function (row) {
        row.classList.remove('active');
        row.setAttribute('aria-pressed', 'false');
      });
    }
    node('traceLocationId').value = '';
    node('traceLocationSubmit').disabled = true;
  }

  function renderCandidates(payload) {
    var list = node('traceLocationCandidates');
    clearSelection();
    list.replaceChildren();
    (payload.candidates || []).forEach(function (candidate) {
      var item = document.createElement('button');
      var title = document.createElement('span');
      item.type = 'button';
      // One line per Location keeps a full page of candidates and the Save button in view together.
      item.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-baseline gap-3';
      item.setAttribute('aria-pressed', 'false');
      item.dataset.candidateId = candidate.id;
      title.textContent = candidate.name;
      item.appendChild(title);
      if (candidate.parent) {
        var parent = document.createElement('span');
        parent.className = 'text-secondary small text-end';
        parent.textContent = 'In ' + candidate.parent;
        item.appendChild(parent);
      }
      item.addEventListener('click', function () {
        Array.prototype.forEach.call(list.children, function (row) {
          row.classList.remove('active');
          row.setAttribute('aria-pressed', 'false');
        });
        item.classList.add('active');
        item.setAttribute('aria-pressed', 'true');
        node('traceLocationId').value = candidate.id;
        node('traceLocationSubmit').disabled = false;
      });
      list.appendChild(item);
    });
    var count = node('traceLocationCount');
    count.textContent = (payload.shown || 0) + ' of ' + (payload.total || 0) + ' visible Locations';
    show(count, true);
  }

  function reportFailure(message) {
    var error = node('traceLocationError');
    clearSelection();
    node('traceLocationCandidates').replaceChildren();
    show(node('traceLocationCount'), false);
    error.textContent = message;
    show(error, true);
  }

  var candidates = window.ndiPickerSearch({
    form: function () { return node('traceLocationForm'); },
    search: function () { return node('traceLocationSearch'); },
    url: function (form, asked) {
      return form.dataset.candidatesUrl + '?location_key=' + encodeURIComponent(node('traceLocationKey').value)
        + '&search=' + encodeURIComponent(asked)
        + '&preview_revision=' + encodeURIComponent(form.elements.namedItem('preview_revision').value);
    },
    onResult: function (payload) {
      show(node('traceLocationError'), false);
      renderCandidates(payload);
    },
    onError: reportFailure
  });

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-trace-location-picker]');
    if (!trigger) return;
    var modal = node('traceLocationPicker');
    if (!modal || !node('traceLocationForm')) return;
    var ModalClass = (typeof bootstrap !== 'undefined' && bootstrap.Modal) || window.Modal;
    if (!ModalClass) return;
    node('traceLocationKey').value = trigger.dataset.traceLocationPicker || '';
    node('traceLocationLabel').textContent = trigger.dataset.traceLocationLabel || '';
    node('traceLocationSearch').value = '';
    show(node('traceLocationError'), false);
    clearSelection();
    node('traceLocationCandidates').replaceChildren();
    show(node('traceLocationCount'), false);
    candidates.load();
    modal.addEventListener('hidden.bs.modal', function () { trigger.focus(); }, {once: true});
    ModalClass.getOrCreateInstance(modal).show(trigger);
  });

  document.addEventListener('keydown', function (event) {
    if (event.target.id === 'traceLocationSearch' && event.key === 'Enter') event.preventDefault();
  });

  document.addEventListener('input', function (event) {
    if (event.target.id !== 'traceLocationSearch') return;
    clearSelection();
    candidates.reschedule(200);
  });
})();
