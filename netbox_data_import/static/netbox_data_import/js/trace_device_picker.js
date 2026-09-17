/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Resolve one source Device label to an operator-selected, server-offered NetBox Device. */
(function () {
  if (window.ndiTraceDevicePicker) return;
  window.ndiTraceDevicePicker = true;


  function node(id) {
    return document.getElementById(id);
  }

  function show(target, visible) {
    if (target) target.hidden = !visible;
  }

  function clearSelection() {
    var list = node('traceDeviceCandidates');
    if (list) {
      Array.prototype.forEach.call(list.children, function (row) {
        row.classList.remove('active');
        row.setAttribute('aria-pressed', 'false');
      });
    }
    node('traceDeviceId').value = '';
    node('traceDeviceOfferedSearch').value = '';
    node('traceDeviceSubmit').disabled = true;
  }

  function hintText(candidate) {
    var parts = [];
    if ((candidate.matched_hints || []).length) {
      parts.push('Matches: ' + candidate.matched_hints.join(', '));
    }
    if ((candidate.conflicting_hints || []).length) {
      parts.push('Differs: ' + candidate.conflicting_hints.join(', '));
    }
    return parts.join('. ');
  }

  function renderCandidates(payload, offered) {
    var list = node('traceDeviceCandidates');
    clearSelection();
    list.replaceChildren();
    (payload.candidates || []).forEach(function (candidate) {
      var item = document.createElement('button');
      var title = document.createElement('div');
      var hints = document.createElement('div');
      item.type = 'button';
      item.className = 'list-group-item list-group-item-action';
      item.setAttribute('aria-pressed', 'false');
      item.dataset.candidateId = candidate.id;
      item.dataset.offeredSearch = offered;
      title.textContent = candidate.display || candidate.name;
      hints.className = 'text-secondary small';
      hints.textContent = hintText(candidate);
      item.appendChild(title);
      if (hints.textContent) item.appendChild(hints);
      item.addEventListener('click', function () {
        Array.prototype.forEach.call(list.children, function (row) {
          row.classList.remove('active');
          row.setAttribute('aria-pressed', 'false');
        });
        item.classList.add('active');
        item.setAttribute('aria-pressed', 'true');
        node('traceDeviceId').value = candidate.id;
        node('traceDeviceOfferedSearch').value = item.dataset.offeredSearch;
        node('traceDeviceSubmit').disabled = false;
      });
      list.appendChild(item);
    });
    var count = node('traceDeviceCount');
    count.textContent = (payload.shown || 0) + ' of ' + (payload.total || 0) + ' eligible';
    show(count, true);
  }

  function reportFailure(message) {
    var error = node('traceDeviceError');
    clearSelection();
    node('traceDeviceCandidates').replaceChildren();
    show(node('traceDeviceCount'), false);
    error.textContent = message;
    show(error, true);
  }

  var candidates = window.ndiPickerSearch({
    form: function () { return node('traceDeviceForm'); },
    search: function () { return node('traceDeviceSearch'); },
    url: function (form, asked) {
      return form.dataset.candidatesUrl + '?device_key=' + encodeURIComponent(node('traceDeviceKey').value)
        + '&search=' + encodeURIComponent(asked)
        + '&preview_revision=' + encodeURIComponent(form.elements.namedItem('preview_revision').value);
    },
    onResult: function (payload, asked) {
      show(node('traceDeviceError'), false);
      renderCandidates(payload, asked);
    },
    onError: reportFailure
  });

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-trace-device-picker]');
    if (!trigger) return;
    var modal = node('traceDevicePicker');
    if (!modal || !node('traceDeviceForm')) return;
    var ModalClass = (typeof bootstrap !== 'undefined' && bootstrap.Modal) || window.Modal;
    if (!ModalClass) return;
    node('traceDeviceKey').value = trigger.dataset.traceDevicePicker || '';
    node('traceDeviceLabel').textContent = trigger.dataset.traceDeviceLabel || '';
    node('traceDeviceSearch').value = '';
    show(node('traceDeviceError'), false);
    clearSelection();
    node('traceDeviceCandidates').replaceChildren();
    show(node('traceDeviceCount'), false);
    candidates.load();
    modal.addEventListener('hidden.bs.modal', function () { trigger.focus(); }, {once: true});
    ModalClass.getOrCreateInstance(modal).show(trigger);
  });

  document.addEventListener('keydown', function (event) {
    if (event.target.id === 'traceDeviceSearch' && event.key === 'Enter') event.preventDefault();
  });

  document.addEventListener('input', function (event) {
    if (event.target.id !== 'traceDeviceSearch') return;
    clearSelection();
    candidates.reschedule(200);
  });
})();
