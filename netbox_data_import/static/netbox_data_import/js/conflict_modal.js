/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Several source columns can fill one Target Field. The modal shows what each column says and
 * saves the one the operator picks as a resolution. */
(function () {
  function readJson(id) {
    var node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : {};
  }

  var CONFLICTS_BY_ROW = readJson('ndi-conflicts-by-row');
  /* Server-rendered from the target-field catalog, so a new field needs no change here. */
  var FIELD_LABELS = readJson('ndi-target-field-labels');

  var conflictModal = document.getElementById('conflictModal');
  if (!conflictModal) return;

  conflictModal.addEventListener('show.bs.modal', function (e) {
    var trigger = e.relatedTarget;
    if (!trigger) return;

    window.ndiConflictModalGeneration = (window.ndiConflictModalGeneration || 0) + 1;
    var form = document.getElementById('conflictForm');
    form.dataset.ndiConflictModalGeneration = window.ndiConflictModalGeneration;
    form.dataset.ndiSubmitting = 'false';
    document.getElementById('conf_source_id').value = trigger.dataset.sourceId || '';

    var conflicts = CONFLICTS_BY_ROW[trigger.dataset.rowNumber] || {};
    var body = document.getElementById('conflictModalBody');
    body.innerHTML = '';

    Object.keys(conflicts).forEach(function (fieldName) {
      var candidates = conflicts[fieldName];
      var section = document.createElement('div');
      section.className = 'mb-3';

      var heading = document.createElement('h6');
      heading.textContent = FIELD_LABELS[fieldName] || fieldName;
      section.appendChild(heading);

      var table = document.createElement('table');
      table.className = 'table table-sm table-bordered mb-0';

      Object.keys(candidates).forEach(function (sourceName) {
        var tr = document.createElement('tr');

        var tdSource = document.createElement('td');
        tdSource.className = 'text-muted';
        tdSource.textContent = sourceName;

        var tdValue = document.createElement('td');
        tdValue.textContent = candidates[sourceName];

        var tdBtn = document.createElement('td');
        tdBtn.style.width = '120px';
        var useBtn = document.createElement('button');
        useBtn.type = 'button';
        useBtn.className = 'btn btn-sm btn-outline-primary ndi-conflict-resolve-btn';
        useBtn.textContent = 'Use this';
        useBtn.dataset.fieldName = fieldName;
        useBtn.dataset.value = candidates[sourceName];
        tdBtn.appendChild(useBtn);

        tr.appendChild(tdSource);
        tr.appendChild(tdValue);
        tr.appendChild(tdBtn);
        table.appendChild(tr);
      });

      section.appendChild(table);
      body.appendChild(section);
    });
  });

  // Bound once: an HTMX swap re-runs this script, and a second listener would post twice.
  if (window.ndiConflictResolveBound) return;
  window.ndiConflictResolveBound = true;

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.ndi-conflict-resolve-btn');
    if (!btn) return;

    var form = document.getElementById('conflictForm');
    if (form.dataset.ndiSubmitting === 'true') return;
    form.dataset.ndiSubmitting = 'true';
    var submissionGeneration = form.dataset.ndiConflictModalGeneration;

    function submissionIsCurrent() {
      var currentForm = document.getElementById('conflictForm');
      return (
        currentForm === form &&
        currentForm.dataset.ndiConflictModalGeneration === submissionGeneration
      );
    }

    document.querySelectorAll('.ndi-conflict-resolve-btn').forEach(function (other) {
      other.disabled = true;
    });
    btn.textContent = 'Saving…';

    var resolved = {};
    resolved[btn.dataset.fieldName] = btn.dataset.value;
    document.getElementById('conf_source_column').value = '_merge_' + btn.dataset.fieldName;
    document.getElementById('conf_original_value').value = '';
    document.getElementById('conf_resolved_fields').value = JSON.stringify(resolved);
    window.ndiPostPreviewAction(form.action, new FormData(form))
      .then(function (payload) {
        window.ndiMarkPreviewStale();
        if (!submissionIsCurrent()) return;
        btn.textContent = 'Saved';
        btn.title = payload.message;
        document.querySelectorAll('.ndi-conflict-resolve-btn').forEach(function (other) {
          other.disabled = other.dataset.fieldName === btn.dataset.fieldName;
        });
        form.dataset.ndiSubmitting = 'false';
      })
      .catch(function (error) {
        if (!submissionIsCurrent()) return;
        form.dataset.ndiSubmitting = 'false';
        document.querySelectorAll('.ndi-conflict-resolve-btn').forEach(function (other) {
          other.disabled = false;
        });
        btn.textContent = 'Use this';
        btn.title = error.message;
      });
  });
})();
