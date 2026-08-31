/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* One source cell often carries two facts, such as "AT900 - host-900". The modal cuts it on a
 * delimiter, lets the operator send each part to a Target Field, and saves the choice as a
 * resolution the next import replays. */
(function () {
  var TARGET_FIELDS = [
    ['', '— ignore —'],
    ['device_name', 'Device name'],
    ['asset_tag', 'Asset tag'],
    ['serial', 'Serial number'],
    ['make', 'Make (manufacturer)'],
    ['model', 'Model (device type)'],
    ['rack_name', 'Rack name'],
  ];

  /* The values this row already carries, so a part can say whether it overwrites one. */
  var fileValues = {};
  var deviceCheckRequest = 0;

  function readJson(id) {
    var node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : {};
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function partIndexes() {
    var container = document.getElementById('res_parts_row');
    if (!container) return [];
    return [...container.querySelectorAll('[id^="res_part_field_"]')].map(function (select) {
      return Number(select.id.slice('res_part_field_'.length));
    });
  }

  function partField(idx) {
    var select = document.getElementById('res_part_field_' + idx);
    return select ? select.value : '';
  }

  function partValue(idx) {
    var input = document.getElementById('res_part_val_' + idx);
    return input ? input.value.trim() : '';
  }

  /* One field takes one part, so a field two parts claim is a choice the operator has to make. */
  function duplicateFields() {
    var seen = {};
    var duplicates = [];
    partIndexes().forEach(function (idx) {
      var field = partField(idx);
      if (!field) return;
      if (seen[field] && duplicates.indexOf(field) === -1) duplicates.push(field);
      seen[field] = true;
    });
    return duplicates;
  }

  function checkPartConflict(idx) {
    var previewDiv = document.getElementById('res_part_preview_' + idx);
    if (!previewDiv) return;
    var fieldSelect = document.getElementById('res_part_field_' + idx);
    var valueInput = document.getElementById('res_part_val_' + idx);
    if (!fieldSelect || !valueInput) return;

    var field = fieldSelect.value;
    var splitValue = valueInput.value.trim();
    var sourceColumn = document.getElementById('res_source_column').value;

    previewDiv.innerHTML = '';
    previewDiv.className = 'mt-2 p-2 rounded small';

    if (!field) {
      previewDiv.style.cssText = '';
      return;
    }

    // Field is the column being split — no conflict possible
    if (field === sourceColumn) {
      previewDiv.style.cssText = 'background:#f8f9fa;border:1px solid #dee2e6;color:#6c757d;';
      previewDiv.textContent = '(Replacing this column — no conflict possible)';
      return;
    }

    var existingValue = (fileValues[field] || '').trim();

    if (!existingValue) {
      previewDiv.style.cssText = 'background:#d1e7dd;border:1px solid #badbcc;color:#0a3622;';
      previewDiv.innerHTML = '<i class="mdi mdi-check-circle-outline"></i> Empty in file — will use split result.';
      return;
    }

    if (existingValue.toLowerCase() === splitValue.toLowerCase()) {
      previewDiv.style.cssText = 'background:#d1e7dd;border:1px solid #badbcc;color:#0a3622;';
      previewDiv.innerHTML =
        '<i class="mdi mdi-check-circle-outline"></i> Matches file value: <code>' + escHtml(existingValue) + '</code>';
      return;
    }

    // Snapshot the force checkbox before innerHTML destroys it, or the acknowledgement is lost.
    var prevCb = document.getElementById('res_force_' + idx);
    var prevChecked = prevCb ? prevCb.checked : false;

    previewDiv.style.cssText = 'background:#f8d7da;border:1px solid #f5c2c7;color:#842029;';
    previewDiv.innerHTML =
      '<strong><i class="mdi mdi-alert-circle-outline"></i> Conflict:</strong> ' +
      'File has <code>' + escHtml(existingValue) + '</code>, ' +
      'split would set <code>' + escHtml(splitValue) + '</code><br>' +
      '<div class="form-check mt-1">' +
        '<input class="form-check-input" type="checkbox" id="res_force_' + idx + '">' +
        '<label class="form-check-label fw-semibold" for="res_force_' + idx + '">' +
          'Force override (use split result, discard file value)' +
        '</label>' +
      '</div>';

    var cb = document.getElementById('res_force_' + idx);
    if (cb) {
      cb.checked = prevChecked;
      cb.addEventListener('change', updateSaveButton);
    }
  }

  function updateSaveButton() {
    var saveBtn = document.querySelector('#splitNameModal button[type="submit"]');
    var conflictAlert = document.getElementById('res_conflict_alert');
    var duplicateAlert = document.getElementById('res_duplicate_alert');
    if (!document.getElementById('res_parts_row')) return;

    var unresolved = partIndexes().some(function (idx) {
      var cb = document.getElementById('res_force_' + idx);
      return cb && !cb.checked;
    });
    var duplicates = duplicateFields();

    partIndexes().forEach(function (idx) {
      var select = document.getElementById('res_part_field_' + idx);
      if (select) select.classList.toggle('is-invalid', duplicates.indexOf(select.value) !== -1);
    });

    if (saveBtn) {
      saveBtn.disabled = unresolved || duplicates.length > 0;
      saveBtn.classList.toggle('disabled', saveBtn.disabled);
    }
    if (conflictAlert) conflictAlert.classList.toggle('d-none', !unresolved);
    if (duplicateAlert) duplicateAlert.classList.toggle('d-none', duplicates.length === 0);
  }

  /* The device name can sit in any part, so the existence check follows the field, not the order. */
  function updateDeviceCheck() {
    var namePart = partIndexes().find(function (idx) {
      return partField(idx) === 'device_name';
    });
    checkDevice(namePart === undefined ? '' : partValue(namePart));
  }

  function checkDevice(name) {
    var request = ++deviceCheckRequest;
    var div = document.getElementById('res_device_check');
    var msg = document.getElementById('res_device_check_msg');
    if (!div || !msg) return;
    var url = (document.getElementById('splitForm') || {dataset: {}}).dataset.checkDeviceUrl;
    if (!name || name.length < 2 || !url) {
      div.classList.add('d-none');
      return;
    }
    fetch(url + '?name=' + encodeURIComponent(name))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (request !== deviceCheckRequest) return;
        div.classList.remove('d-none');
        msg.textContent = '';
        var icon = document.createElement('i');
        var strong = document.createElement('strong');
        strong.textContent = name;
        if (data.exists) {
          icon.className = 'mdi mdi-check-circle text-success';
          msg.appendChild(icon);
          msg.appendChild(document.createTextNode(' Device '));
          msg.appendChild(strong);
          msg.appendChild(document.createTextNode(' already exists in NetBox — '));
          if (data.count > 1) {
            msg.appendChild(document.createTextNode(data.count + ' matches'));
          } else {
            var link = document.createElement('a');
            link.href = data.url;
            link.target = '_blank';
            link.textContent = 'view it';
            msg.appendChild(link);
          }
        } else {
          icon.className = 'mdi mdi-plus-circle text-muted';
          msg.appendChild(icon);
          msg.appendChild(document.createTextNode(' Device '));
          msg.appendChild(strong);
          msg.appendChild(document.createTextNode(' not yet in NetBox — will be created on import.'));
        }
      })
      .catch(function () {
        if (request === deviceCheckRequest) div.classList.add('d-none');
      });
  }

  function addPart(container, idx, value, defaultField) {
    var col = document.createElement('div');
    col.className = 'col-md-6';
    col.id = 'res_part_col_' + idx;

    var label = document.createElement('label');
    label.className = 'form-label';
    label.textContent = 'Part ' + (idx + 1);

    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control mb-1';
    input.id = 'res_part_val_' + idx;
    input.value = value;

    var select = document.createElement('select');
    select.className = 'form-select form-select-sm';
    select.id = 'res_part_field_' + idx;
    TARGET_FIELDS.forEach(function (f) {
      var opt = document.createElement('option');
      opt.value = f[0];
      opt.textContent = f[1];
      if (f[0] === (defaultField || '')) opt.selected = true;
      select.appendChild(opt);
    });

    var previewDiv = document.createElement('div');
    previewDiv.id = 'res_part_preview_' + idx;

    input.addEventListener('input', function () {
      checkPartConflict(idx);
      updateSaveButton();
      updateDeviceCheck();
    });
    select.addEventListener('change', function () {
      checkPartConflict(idx);
      updateSaveButton();
      updateDeviceCheck();
    });

    col.appendChild(label);
    col.appendChild(input);
    col.appendChild(select);
    col.appendChild(previewDiv);
    container.appendChild(col);
  }

  /* `resolvedFields` is a saved resolution when the operator reopens a row, empty otherwise. */
  function renderParts(originalValue, resolvedFields) {
    var container = document.getElementById('res_parts_row');
    if (!container) return;
    var delimiter = document.getElementById('res_delimiter').value || ' - ';
    var parts = originalValue.split(delimiter);
    var defaultFields = ['asset_tag', 'device_name'];
    var entries = Object.entries(resolvedFields || {});
    container.innerHTML = '';

    parts.forEach(function (part, idx) {
      var field = entries[idx] ? entries[idx][0] : (defaultFields[idx] || '');
      var value = entries[idx] ? entries[idx][1] : part.trim();
      addPart(container, idx, value, field);
    });
    partIndexes().forEach(checkPartConflict);
    updateSaveButton();
    updateDeviceCheck();
  }

  function buildResolvedFields() {
    if (duplicateFields().length) return false;
    var unresolved = partIndexes().some(function (idx) {
      var cb = document.getElementById('res_force_' + idx);
      return cb && !cb.checked;
    });
    if (unresolved) return false;

    var fields = {};
    partIndexes().forEach(function (idx) {
      var value = partValue(idx);
      var field = partField(idx);
      if (field && value) fields[field] = value;
    });
    document.getElementById('res_resolved_fields').value = JSON.stringify(fields);
    return true;
  }

  function clearSaveError() {
    var saveError = document.getElementById('res_save_error');
    saveError.textContent = '';
    saveError.classList.add('d-none');
  }

  // Bound once: an HTMX swap re-runs this script, and a second listener would render twice.
  if (window.ndiSplitNameModalBound) return;
  window.ndiSplitNameModalBound = true;

  document.addEventListener('show.bs.modal', function (event) {
    // No trigger means no row to read: leave the form empty and let the server refuse it.
    if (event.target.id !== 'splitNameModal' || !event.relatedTarget) return;
    var btn = event.relatedTarget;
    var sourceId = btn.dataset.sourceId;
    var sourceColumn = btn.dataset.sourceColumn;
    document.getElementById('res_source_id').value = sourceId;
    document.getElementById('res_source_column').value = sourceColumn;
    document.getElementById('res_original_value').value = btn.dataset.originalValue;
    document.getElementById('res_original_display').textContent = btn.dataset.originalValue;

    fileValues = readJson('ndi-split-field-values')[sourceId] || {};

    var existing = ((window.EXISTING_RESOLUTIONS || {})[sourceId] || {})[sourceColumn];
    document.getElementById('res_existing_notice').classList.toggle('d-none', !existing);
    if (existing) {
      document.getElementById('res_existing_display').textContent = JSON.stringify(existing.resolved_fields);
    }
    renderParts(btn.dataset.originalValue, existing ? existing.resolved_fields : null);
    var saveBtn = document.querySelector('#splitForm button[type="submit"]');
    saveBtn.textContent = 'Save resolution';
    saveBtn.removeAttribute('title');
    clearSaveError();
  });

  document.addEventListener('input', function (event) {
    if (event.target.id !== 'res_delimiter') return;
    renderParts(document.getElementById('res_original_value').value, null);
  });

  document.addEventListener('submit', function (event) {
    if (event.target.id !== 'splitForm') return;
    if (!buildResolvedFields()) {
      event.preventDefault();
      updateSaveButton();
      return;
    }
    if (typeof window.ndiPostPreviewAction !== 'function') return;
    event.preventDefault();
    var form = event.target;
    var saveBtn = form.querySelector('button[type="submit"]');
    clearSaveError();
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    window.ndiPostPreviewAction(form.action, new FormData(form))
      .then(function (payload) {
        saveBtn.textContent = 'Saved';
        saveBtn.title = payload.message;
        window.ndiMarkPreviewStale();
      })
      .catch(function (error) {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save resolution';
        var saveError = document.getElementById('res_save_error');
        saveError.textContent = error.message || 'Could not save the resolution.';
        saveError.classList.remove('d-none');
      });
  });
})();
