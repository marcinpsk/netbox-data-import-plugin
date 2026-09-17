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

  /* Clicking anywhere on a source row toggles its detail row. This is a pointer shortcut for the
   * row's own toggle button: the row keeps its table semantics, so the button carries the
   * keyboard and the assistive-technology contract. Controls inside the row keep their behavior. */
  document.addEventListener('click', function (event) {
    var target = event.target;
    if (target.closest('.ndi-diff-toggle')) return;
    if (target.closest('button, a, input, select, textarea, label, [data-ndi-modal]')) return;
    var row = target.closest('#previewRowsBody > tr[data-action]');
    if (!row) return;
    // The detail row always follows its source row, so no id lookup is involved.
    var diffRow = row.nextElementSibling;
    if (!diffRow || !diffRow.classList.contains('ndi-diff-row')) return;
    setDiffExpanded(diffRow, diffRow.hidden);
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

  /* The chosen racks, read from the options rather than the control, so an enhanced select
   * and a plain one answer alike. */
  function selectedRacks() {
    var rackSelect = document.getElementById('previewRackFilter');
    var chosen = [];
    if (!rackSelect) return chosen;
    // Tom Select drops an option whose value is empty, so the page names that option instead.
    var noRackValue = rackSelect.dataset.noRackValue;
    for (var index = 0; index < rackSelect.options.length; index++) {
      if (!rackSelect.options[index].selected) continue;
      var value = rackSelect.options[index].value;
      chosen.push(value === noRackValue ? '' : value);
    }
    return chosen;
  }

  function applyFilters() {
    var filterInput = document.getElementById('previewRowFilter');
    var actionSelect = document.getElementById('previewActionFilter');
    var text = (filterInput ? filterInput.value : '').toLowerCase().trim();
    var action = (actionSelect ? actionSelect.value : '').toLowerCase();
    var racks = selectedRacks();
    var clearButton = document.getElementById('previewRowFilterClear');
    if (clearButton) clearButton.style.display = (text || action || racks.length) ? '' : 'none';

    // Source rows only: field-difference rows hold sub-tables with rows of their own, and the
    // empty-state rows belong to the table rather than to the file.
    var rows = document.querySelectorAll('#previewRowsBody > tr[data-action]');
    var shown = 0;
    var hiddenErrors = 0;
    rows.forEach(function (row) {
      var textMatch = !text || row.textContent.toLowerCase().includes(text);
      var rowAction = (row.dataset.action || '').toLowerCase();
      // An empty option value is the rows that name no rack, as the rack view groups them.
      var rackMatch = !racks.length || racks.indexOf(row.dataset.rackName || '') !== -1;
      var visible = textMatch && (!action || rowAction === action) && rackMatch;
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

  function setFilters(text, action, racks) {
    var filterInput = document.getElementById('previewRowFilter');
    var actionSelect = document.getElementById('previewActionFilter');
    var rackSelect = document.getElementById('previewRackFilter');
    var chosen = racks || [];
    if (filterInput) filterInput.value = text;
    if (actionSelect) {
      actionSelect.value = action;
      // NetBox replaces the select with a Tom Select control that reads its own value.
      if (actionSelect.tomselect) actionSelect.tomselect.setValue(action, true);
    }
    if (rackSelect) {
      for (var index = 0; index < rackSelect.options.length; index++) {
        rackSelect.options[index].selected = chosen.indexOf(rackSelect.options[index].value) !== -1;
      }
      if (rackSelect.tomselect) rackSelect.tomselect.setValue(chosen, true);
    }
    applyFilters();
  }

  document.addEventListener('input', function (event) {
    if (event.target.id === 'previewRowFilter') applyFilters();
  });

  document.addEventListener('change', function (event) {
    if (event.target.id === 'previewActionFilter' || event.target.id === 'previewRackFilter') applyFilters();
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

  /* A recalculation reloads the whole page, so the filters and the place the operator was reading
   * are carried across it. The entry is consumed on arrival, so only that reload is moved. */
  var VIEW_KEY = 'ndi-preview-view';

  /* The first row still on screen. A recalculated preview can hold a different number of rows,
   * so an offset alone would land somewhere else. */
  function rowInView() {
    var rows = document.querySelectorAll('#previewRowsBody > tr[data-action]');
    for (var index = 0; index < rows.length; index++) {
      if (rows[index].style.display === 'none') continue;
      var rect = rows[index].getBoundingClientRect();
      // Both edges, so a table entirely below the fold does not answer with its first row.
      if (rect.bottom > 0 && rect.top < (window.innerHeight || 0)) {
        return {row: rows[index].dataset.rowNumber || '', type: rows[index].dataset.objectType || ''};
      }
    }
    return null;
  }

  function findRow(anchor) {
    if (!anchor) return null;
    var rows = document.querySelectorAll('#previewRowsBody > tr[data-action]');
    for (var index = 0; index < rows.length; index++) {
      if (rows[index].dataset.rowNumber === anchor.row && rows[index].dataset.objectType === anchor.type) {
        return rows[index];
      }
    }
    return null;
  }

  function rememberView() {
    var filterInput = document.getElementById('previewRowFilter');
    var actionSelect = document.getElementById('previewActionFilter');
    try {
      window.sessionStorage.setItem(VIEW_KEY, JSON.stringify({
        text: filterInput ? filterInput.value : '',
        action: actionSelect ? actionSelect.value : '',
        scrollY: window.scrollY || 0,
        anchor: rowInView()
      }));
    } catch (error) {
      /* The recalculation still runs. Only the restore is lost. */
    }
  }

  function restoreView() {
    var stored = null;
    try {
      stored = window.sessionStorage.getItem(VIEW_KEY);
      window.sessionStorage.removeItem(VIEW_KEY);
    } catch (error) {
      return;
    }
    if (!stored) return;
    var view;
    try {
      view = JSON.parse(stored);
    } catch (error) {
      return;
    }
    if (view.text || view.action) setFilters(view.text || '', view.action || '');
    var target = findRow(view.anchor);
    if (target && target.style.display !== 'none') {
      target.scrollIntoView();
      return;
    }
    if (view.scrollY) window.scrollTo(0, view.scrollY);
  }

  window.ndiRememberPreviewView = rememberView;
  window.ndiRestorePreviewView = restoreView;

  // A direct press navigates without the row-action script, so the view is stored here too.
  document.addEventListener('click', function (event) {
    if (event.target.closest('.ndi-recalculate-preview')) rememberView();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', restoreView);
  } else {
    restoreView();
  }
}());
