/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Row controls that must work from the first row the browser paints: a large preview streams
 * for several seconds before the scripts at the end of the page run. Every listener here
 * delegates from `document`, so it covers rows that arrive later.
 *
 * Row buttons open their modal from here instead of through `data-bs-toggle="modal"`. NetBox
 * constructs one Bootstrap Modal per trigger at load, which costs seconds of blocked main
 * thread on a preview with thousands of rows. */
(function () {
  // The script ships inside the swapped content, so an htmx boost evaluates it again on every
  // navigation. Document listeners outlive the swap, so a second evaluation would double them.
  if (window.ndiPreviewRowControls) return;
  window.ndiPreviewRowControls = true;

  function setDiffExpanded(diffRow, expanded) {
    if (!diffRow) return;
    diffRow.hidden = !expanded;
    var toggles = document.querySelectorAll('[data-diff-target="' + diffRow.id + '"]');
    for (var index = 0; index < toggles.length; index++) {
      toggles[index].setAttribute('aria-expanded', expanded ? 'true' : 'false');
      var icon = toggles[index].querySelector('.mdi');
      if (!icon) continue;
      icon.classList.toggle('mdi-chevron-down', !expanded);
      icon.classList.toggle('mdi-chevron-up', expanded);
    }
  }

  window.ndiSetDiffExpanded = setDiffExpanded;

  document.addEventListener('click', function (event) {
    var toggle = event.target.closest('.ndi-diff-toggle');
    if (!toggle) return;
    var diffRow = document.getElementById(toggle.dataset.diffTarget);
    if (!diffRow) return;
    setDiffExpanded(diffRow, diffRow.hidden);
  });

  /* The whole source row toggles its own detail row, so a row with nothing actionable still
   * answers a click. Controls inside the row keep their own behavior. */
  function toggleRow(target) {
    if (target.closest('.ndi-diff-toggle')) return;
    if (target.closest('button, a, input, select, textarea, label, [data-ndi-modal]')) return;
    var row = target.closest('#previewRowsBody > tr[data-action]');
    if (!row) return;
    // The detail row always follows its source row. Row ids repeat across object types, so
    // getElementById would resolve the wrong one.
    var diffRow = row.nextElementSibling;
    if (!diffRow || !diffRow.classList.contains('ndi-diff-row')) return;
    setDiffExpanded(diffRow, diffRow.hidden);
    row.setAttribute('aria-expanded', diffRow.hidden ? 'false' : 'true');
  }

  document.addEventListener('click', function (event) {
    toggleRow(event.target);
  });

  // A row carries tabindex, so Enter and Space have to reach the same detail row.
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    if (!event.target.matches('#previewRowsBody > tr[data-action]')) return;
    event.preventDefault();
    toggleRow(event.target);
  });

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-ndi-modal]');
    if (!trigger || trigger.disabled) return;
    var target = document.querySelector(trigger.dataset.ndiModal);
    // NetBox/Tabler exposes Bootstrap as global `Modal`, not `bootstrap.Modal`.
    var ModalClass = (typeof bootstrap !== 'undefined' && bootstrap.Modal) || window.Modal;
    if (!target || !ModalClass) return;
    event.preventDefault();
    target.addEventListener('hidden.bs.modal', function () { trigger.focus(); }, {once: true});
    ModalClass.getOrCreateInstance(target).show(trigger);
  });

  function applyFilters() {
    var filterInput = document.getElementById('previewRowFilter');
    var actionSelect = document.getElementById('previewActionFilter');
    var text = (filterInput ? filterInput.value : '').toLowerCase().trim();
    var action = (actionSelect ? actionSelect.value : '').toLowerCase();
    var clearButton = document.getElementById('previewRowFilterClear');
    if (clearButton) clearButton.style.display = (text || action) ? '' : 'none';

    // Source rows only: field-difference rows hold sub-tables with rows of their own, and the
    // empty-state rows belong to the table rather than to the file.
    var rows = document.querySelectorAll('#previewRowsBody > tr[data-action]');
    var shown = 0;
    var hiddenErrors = 0;
    rows.forEach(function (row) {
      var textMatch = !text || row.textContent.toLowerCase().includes(text);
      var rowAction = (row.dataset.action || '').toLowerCase();
      var visible = textMatch && (!action || rowAction === action);
      row.style.display = visible ? '' : 'none';
      if (visible) shown++;
      else if (rowAction === 'error') hiddenErrors++;
      // A field-difference row belongs to the row above it and collapses with it.
      var diffRow = row.nextElementSibling;
      if (!visible && diffRow && diffRow.classList.contains('ndi-diff-row') && !diffRow.hidden) {
        setDiffExpanded(diffRow, false);
      }
    });

    var noResults = document.getElementById('previewNoFilterResults');
    if (noResults) noResults.style.display = shown === 0 ? '' : 'none';
    var warning = document.getElementById('ndi-hidden-err-warn');
    if (warning) {
      warning.style.display = hiddenErrors > 0 ? '' : 'none';
      var count = document.getElementById('ndi-hidden-err-count');
      if (count) count.textContent = hiddenErrors;
    }
  }

  function setFilters(text, action) {
    var filterInput = document.getElementById('previewRowFilter');
    var actionSelect = document.getElementById('previewActionFilter');
    if (filterInput) filterInput.value = text;
    if (actionSelect) {
      actionSelect.value = action;
      // NetBox replaces the select with a Tom Select control that reads its own value.
      if (actionSelect.tomselect) actionSelect.tomselect.setValue(action, true);
    }
    applyFilters();
  }

  document.addEventListener('input', function (event) {
    if (event.target.id === 'previewRowFilter') applyFilters();
  });

  document.addEventListener('change', function (event) {
    if (event.target.id === 'previewActionFilter') applyFilters();
  });

  document.addEventListener('keydown', function (event) {
    if (event.target.id === 'previewRowFilter' && event.key === 'Escape') setFilters('', '');
  });

  document.addEventListener('click', function (event) {
    if (event.target.closest('#previewRowFilterClear')) setFilters('', '');
    var showErrors = event.target.closest('#ndi-show-errors-link');
    if (showErrors) {
      event.preventDefault();
      setFilters('', 'error');
    }
  });
}());
