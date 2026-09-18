/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The per-row "Sync to NetBox" confirmation: it shows what the row would write, posts the write,
 * and then either recalculates the preview or reports that one is due. */
(function () {
  var modal = document.getElementById('syncRowModal');
  if (!modal) return;

  var RECALCULATE_CHOICE_KEY = 'ndi-sync-recalculate';

  var currentRowNumber = null;
  var currentSyncButton = null;
  var currentSyncRequest = null;
  var pendingSyncRequests = new WeakMap();
  var syncsInFlight = 0;
  var syncWritePending = false;
  var recalculateChoice = document.getElementById('syncRowRecalculate');

  function readJson(id) {
    var node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : {};
  }

  /* The choice lasts for this tab only. A browser that refuses storage keeps the default. */
  function storedChoice() {
    try {
      return window.sessionStorage.getItem(RECALCULATE_CHOICE_KEY);
    } catch (error) {
      return null;
    }
  }

  function rememberChoice(checked) {
    try {
      window.sessionStorage.setItem(RECALCULATE_CHOICE_KEY, checked ? 'on' : 'off');
    } catch (error) {
      /* The choice still holds for this page. */
    }
  }

  if (recalculateChoice) {
    recalculateChoice.checked = storedChoice() !== 'off';
    recalculateChoice.addEventListener('change', function () {
      rememberChoice(recalculateChoice.checked);
    });
  }

  /* A write the operator cannot see the result of is the reason this exists, so the preview is
   * recalculated once the last request settles, never while another one is still in flight.
   * The last request to settle owns the recalculation even when it is the one that failed. */
  function recalculateAfterSync() {
    if (!syncWritePending) return false;
    if (!recalculateChoice || !recalculateChoice.checked) return false;
    if (syncsInFlight > 0) return false;
    if (typeof window.ndiRecalculatePreview !== 'function') return false;
    syncWritePending = false;
    return window.ndiRecalculatePreview();
  }

  function pendingWriteSummary(btn) {
    var pendingWrites = [];
    if (btn.dataset.pendingWriteContact === 'true') pendingWrites.push('contact data');
    if (btn.dataset.pendingWriteProvenance === 'true') pendingWrites.push('provenance data');
    return pendingWrites.length ? ' Sync will also write ' + pendingWrites.join(' and ') + '.' : '';
  }

  modal.addEventListener('show.bs.modal', function (e) {
    var btn = e.relatedTarget;
    // An open with no trigger must not inherit the row the last open left behind.
    currentRowNumber = '';
    currentSyncButton = null;
    currentSyncRequest = null;
    if (!btn) return;
    currentRowNumber = btn.dataset.rowNumber;
    currentSyncButton = btn;
    currentSyncRequest = pendingSyncRequests.get(btn) || null;

    document.getElementById('syncRowName').textContent = btn.dataset.name || '—';
    document.getElementById('syncRowNumber').textContent = currentRowNumber || '—';
    document.getElementById('syncRowSourceId').textContent = btn.dataset.sourceId || '—';
    var verb = btn.dataset.action === 'update' ? 'Update' : 'Create';
    document.getElementById('syncRowBadge').textContent = verb + ' ' + (btn.dataset.objectType || '');

    var resolutions = window.EXISTING_RESOLUTIONS || {};
    var rowRes = resolutions[btn.dataset.sourceId] || {};
    var resolvedFieldKeys = {};
    for (var col in rowRes) {
      var resolved = rowRes[col].resolved_fields || {};
      for (var f in resolved) {
        resolvedFieldKeys[f] = true;
      }
    }

    function resolutionBadge(fieldKey) {
      if (!resolvedFieldKeys[fieldKey]) return null;
      var badge = document.createElement('span');
      badge.className = 'badge text-bg-success ms-1';
      badge.textContent = 'from resolution';
      return badge;
    }

    var tbody = document.getElementById('syncRowFields');
    var summary = document.getElementById('syncRowSummary');
    var showUnchanged = document.getElementById('syncRowShowUnchanged');
    tbody.innerHTML = '';

    function addCell(tr, text, className) {
      var td = document.createElement('td');
      if (className) td.className = className;
      td.appendChild(document.createTextNode(text === '' || text == null ? '\u2014' : text));
      tr.appendChild(td);
      return td;
    }

    /* The operator is about to write to NetBox, so each field says what the write does to it. */
    var STATE_LABELS = {
      change: ['will update', 'text-bg-warning'],
      unchanged: ['unchanged', 'text-bg-light text-muted'],
      ignored: ['ignored', 'text-bg-secondary'],
      not_written: ['not written', 'text-bg-secondary']
    };

    // Row numbers repeat across object types, so the key names the type as well as the number.
    var previewKey = (btn.dataset.objectType || '') + ':' + currentRowNumber;
    var changePreview = readJson('ndi-sync-change-preview-by-row')[previewKey] || [];
    var isUpdate = btn.dataset.action === 'update';
    var reviewed = isUpdate && changePreview.length > 0;
    var unchangedRows = [];

    if (reviewed) {
      changePreview.forEach(function (entry) {
        var tr = document.createElement('tr');
        addCell(tr, entry.label, 'fw-semibold');
        addCell(tr, entry.netbox, entry.state === 'change' ? '' : 'text-muted');
        var after = addCell(tr, entry.state === 'change' ? entry.file : entry.netbox,
                            entry.state === 'change' ? 'fw-semibold' : 'text-muted');
        var state = STATE_LABELS[entry.state] || STATE_LABELS.unchanged;
        var badge = document.createElement('span');
        badge.className = 'badge ms-1 ' + state[1];
        badge.textContent = state[0];
        after.appendChild(badge);
        /* A value the writer will not send is shown struck through, so it reads as not applied. */
        if (entry.state === 'ignored' || entry.state === 'not_written') {
          var skipped = document.createElement('span');
          skipped.className = 'text-muted small d-block text-decoration-line-through';
          skipped.textContent = entry.file;
          after.appendChild(skipped);
        }
        var resolved = resolutionBadge(entry.field);
        if (resolved) after.appendChild(resolved);
        if (entry.state === 'unchanged') {
          tr.hidden = true;
          unchangedRows.push(tr);
        }
        tbody.appendChild(tr);
      });
    } else {
      /* A create has nothing in NetBox yet, and a rack row carries no field review. */
      var fieldDefs = [
        ['Name', btn.dataset.name, 'device_name'],
        ['Rack', btn.dataset.rackName, 'rack_name'],
        ['Source ID', btn.dataset.sourceId, 'source_id'],
        ['Manufacturer', btn.dataset.sourceMake, 'source_make'],
        ['Model', btn.dataset.sourceModel, 'source_model'],
        ['Asset tag', btn.dataset.assetTag, 'asset_tag'],
        ['Rack type', btn.dataset.rackTypeName, 'rack_type'],
        ['Serial', btn.dataset.serial, 'serial'],
        ['U Position', btn.dataset.uPosition, 'u_position'],
        ['U Height', btn.dataset.uHeight, 'u_height'],
        ['Face', btn.dataset.face, 'face'],
        ['Airflow', btn.dataset.airflow, 'airflow'],
        ['Status', btn.dataset.status, 'status']
      ];
      fieldDefs.forEach(function (def) {
        if (!def[1]) return;
        var tr = document.createElement('tr');
        addCell(tr, def[0], 'fw-semibold');
        addCell(tr, '', 'text-muted');
        var after = addCell(tr, def[1]);
        var resolved = resolutionBadge(def[2]);
        if (resolved) after.appendChild(resolved);
        tbody.appendChild(tr);
      });
    }

    var counts = {change: 0, unchanged: 0, ignored: 0, not_written: 0};
    changePreview.forEach(function (entry) {
      if (entry.state in counts) counts[entry.state] += 1;
    });
    /* Ignored and not written are separate states, so one count cannot stand for both. */
    var skipped = [];
    if (counts.ignored) skipped.push(counts.ignored + ' ignored');
    if (counts.not_written) skipped.push(counts.not_written + ' not written');
    var skippedPhrase = skipped.join(', ');

    if (showUnchanged) {
      showUnchanged.parentNode.hidden = unchangedRows.length === 0;
      showUnchanged.checked = false;
      showUnchanged.onchange = function () {
        unchangedRows.forEach(function (tr) { tr.hidden = !showUnchanged.checked; });
      };
    }

    document.getElementById('syncRowNextHead').textContent = reviewed ? 'After sync' : 'Will be set to';

    if (!reviewed) {
      summary.className = 'small mb-2 text-muted';
      summary.textContent = isUpdate
        ? 'This row has no reviewed field differences.'
        : 'Creates this ' + (btn.dataset.objectType || 'object') + ' in NetBox with the values below.';
    } else if (counts.change === 0) {
      summary.className = 'small mb-2 text-muted';
      var pendingWrites = pendingWriteSummary(btn);
      if (skippedPhrase) {
        summary.textContent = 'No reviewed device fields will change. ' + counts.unchanged
          + ' unchanged, ' + skippedPhrase + '.' + pendingWrites;
      } else {
        summary.textContent = pendingWrites
          ? 'No reviewed device fields will change.' + pendingWrites
          : 'No reviewed device fields will change. NetBox already holds every reviewed value.';
      }
    } else {
      summary.className = 'small mb-2';
      summary.textContent = counts.change + ' field' + (counts.change === 1 ? '' : 's')
        + ' will change. ' + counts.unchanged + ' unchanged'
        + (skippedPhrase ? ', ' + skippedPhrase + '.' : '.');
    }

    // Append extra_columns (custom fields / unmapped columns) below standard fields
    var extraCols = readJson('ndi-extra-columns-by-row')[previewKey] || {};
    for (var ecKey in extraCols) {
      var ecVal = String(extraCols[ecKey]);
      if (!ecVal) continue;
      var ecTr = document.createElement('tr');
      addCell(ecTr, ecKey, 'text-muted small');
      addCell(ecTr, '', 'text-muted');
      addCell(ecTr, ecVal);
      tbody.appendChild(ecTr);
    }

    if (btn.dataset.detail) {
      var detailTr = document.createElement('tr');
      addCell(detailTr, 'Detail', 'fw-semibold');
      var detailTd = document.createElement('td');
      detailTd.colSpan = 2;
      detailTd.textContent = btn.dataset.detail;
      detailTr.appendChild(detailTd);
      tbody.appendChild(detailTr);
    }

    var errorDiv = document.getElementById('syncRowError');
    errorDiv.textContent = '';
    errorDiv.classList.add('d-none');

    var confirmBtn = document.getElementById('syncRowConfirm');
    var rowIsPending = pendingSyncRequests.has(btn);
    confirmBtn.disabled = rowIsPending;
    confirmBtn.querySelector('.ndi-sync-row-idle').classList.toggle('d-none', rowIsPending);
    confirmBtn.querySelector('.ndi-sync-row-loading').classList.toggle('d-none', !rowIsPending);
  });

  document.getElementById('syncRowConfirm').addEventListener('click', function () {
    if (!currentSyncButton || pendingSyncRequests.has(currentSyncButton)) return;
    var confirmBtn = this;
    confirmBtn.disabled = true;
    confirmBtn.querySelector('.ndi-sync-row-idle').classList.add('d-none');
    confirmBtn.querySelector('.ndi-sync-row-loading').classList.remove('d-none');

    var errorDiv = document.getElementById('syncRowError');
    errorDiv.classList.add('d-none');

    if (typeof window.ndiPostPreviewAction !== 'function') {
      confirmBtn.disabled = false;
      confirmBtn.querySelector('.ndi-sync-row-idle').classList.remove('d-none');
      confirmBtn.querySelector('.ndi-sync-row-loading').classList.add('d-none');
      errorDiv.textContent = 'The preview action script is unavailable. Reload the page.';
      errorDiv.classList.remove('d-none');
      return;
    }

    var submittedRowNumber = currentRowNumber;
    var submittedSyncButton = currentSyncButton;
    var submittedSyncRequest = {};
    currentSyncRequest = submittedSyncRequest;
    pendingSyncRequests.set(submittedSyncButton, submittedSyncRequest);
    submittedSyncButton.disabled = true;
    syncsInFlight += 1;
    var body = new URLSearchParams({row_number: submittedRowNumber});
    window.ndiPostPreviewAction(modal.dataset.syncUrl, body)
    .then(function (data) {
      syncsInFlight -= 1;
      syncWritePending = true;
      submittedSyncButton.disabled = true;
      submittedSyncButton.removeAttribute('data-ndi-modal');
      submittedSyncButton.title = data.message || 'Synced to NetBox.';
      submittedSyncButton.innerHTML = '<i class="mdi mdi-check"></i> Synced';
      if (pendingSyncRequests.get(submittedSyncButton) === submittedSyncRequest) {
        pendingSyncRequests.delete(submittedSyncButton);
      }
      var ownsCurrentModal = currentSyncRequest === submittedSyncRequest
        && currentSyncButton === submittedSyncButton;
      if (currentSyncRequest === submittedSyncRequest) currentSyncRequest = null;
      if (recalculateAfterSync()) {
        // The page is leaving, so the modal reports the wait rather than closing onto a dead page.
        if (ownsCurrentModal) {
          confirmBtn.querySelector('.ndi-sync-row-loading-label').textContent = 'Recalculating preview…';
        }
        return;
      }
      if (typeof window.ndiMarkPreviewStale === 'function') {
        window.ndiMarkPreviewStale(data.detail);
      }
      if (!ownsCurrentModal) return;
      var ModalClass = (typeof bootstrap !== 'undefined' && bootstrap.Modal) || window.Modal;
      if (ModalClass) {
        ModalClass.getOrCreateInstance(modal).hide();
      }
    })
    .catch(function (error) {
      syncsInFlight -= 1;
      if (pendingSyncRequests.get(submittedSyncButton) === submittedSyncRequest) {
        pendingSyncRequests.delete(submittedSyncButton);
        // markPreviewStale() skips a button that is already disabled, so a write that landed while
        // this one was in flight never latched it. Retrying it would only meet the server refusal.
        if (syncWritePending) {
          submittedSyncButton.title = 'Recalculate the preview before synchronizing a row.';
        } else {
          submittedSyncButton.disabled = false;
        }
      }
      var ownsCurrentModal = currentSyncRequest === submittedSyncRequest
        && currentSyncButton === submittedSyncButton;
      if (currentSyncRequest === submittedSyncRequest) currentSyncRequest = null;
      recalculateAfterSync();
      if (!ownsCurrentModal) return;
      confirmBtn.disabled = false;
      confirmBtn.querySelector('.ndi-sync-row-idle').classList.remove('d-none');
      confirmBtn.querySelector('.ndi-sync-row-loading').classList.add('d-none');
      errorDiv.textContent = error.message || 'Sync failed';
      errorDiv.classList.remove('d-none');
    });
  });
}());
