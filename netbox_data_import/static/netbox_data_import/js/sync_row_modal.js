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
   * recalculated once the last write lands, never while another one is still in flight. */
  function recalculateAfterSync() {
    if (!recalculateChoice || !recalculateChoice.checked) return false;
    if (syncsInFlight > 0) return false;
    if (typeof window.ndiRecalculatePreview !== 'function') return false;
    return window.ndiRecalculatePreview();
  }

  modal.addEventListener('show.bs.modal', function (e) {
    var btn = e.relatedTarget;
    if (!btn) return;
    currentRowNumber = btn.dataset.rowNumber;
    currentSyncButton = btn;
    currentSyncRequest = pendingSyncRequests.get(btn) || null;

    document.getElementById('syncRowName').textContent = btn.dataset.name || '—';
    document.getElementById('syncRowNumber').textContent = currentRowNumber || '—';
    document.getElementById('syncRowSourceId').textContent = btn.dataset.sourceId || '—';
    document.getElementById('syncRowBadge').textContent = 'Create ' + (btn.dataset.objectType || '');

    var resolutions = window.EXISTING_RESOLUTIONS || {};
    var rowRes = resolutions[btn.dataset.sourceId] || {};
    var resolvedFieldKeys = {};
    for (var col in rowRes) {
      var resolved = rowRes[col].resolved_fields || {};
      for (var f in resolved) {
        resolvedFieldKeys[f] = true;
      }
    }

    function makeValueCell(value, fieldKey) {
      var td = document.createElement('td');
      td.appendChild(document.createTextNode(value || '—'));
      if (resolvedFieldKeys[fieldKey]) {
        var badge = document.createElement('span');
        badge.className = 'badge text-bg-success ms-1';
        badge.textContent = 'from resolution';
        td.appendChild(badge);
      }
      return td;
    }

    var tbody = document.getElementById('syncRowFields');
    tbody.innerHTML = '';

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
      ['Status', btn.dataset.status, 'status'],
    ];

    fieldDefs.forEach(function (def) {
      var label = def[0], value = def[1], fieldKey = def[2];
      if (!value) return;
      var tr = document.createElement('tr');
      var th = document.createElement('td');
      th.className = 'fw-semibold';
      th.textContent = label;
      tr.appendChild(th);
      tr.appendChild(makeValueCell(value, fieldKey));
      tbody.appendChild(tr);
    });

    // Append extra_columns (custom fields / unmapped columns) below standard fields
    var extraCols = readJson('ndi-extra-columns-by-row')[btn.dataset.rowNumber] || {};
    for (var ecKey in extraCols) {
      var ecVal = String(extraCols[ecKey]);
      if (!ecVal) continue;
      var ecTr = document.createElement('tr');
      var ecTd1 = document.createElement('td');
      ecTd1.className = 'text-muted small';
      ecTd1.textContent = ecKey;
      var ecTd2 = document.createElement('td');
      ecTd2.textContent = ecVal;
      ecTr.appendChild(ecTd1);
      ecTr.appendChild(ecTd2);
      tbody.appendChild(ecTr);
    }

    if (btn.dataset.detail) {
      var tr = document.createElement('tr');
      var th = document.createElement('td');
      th.className = 'fw-semibold';
      th.textContent = 'Detail';
      tr.appendChild(th);
      var td = document.createElement('td');
      td.textContent = btn.dataset.detail;
      tr.appendChild(td);
      tbody.appendChild(tr);
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
        submittedSyncButton.disabled = false;
      }
      var ownsCurrentModal = currentSyncRequest === submittedSyncRequest
        && currentSyncButton === submittedSyncButton;
      if (currentSyncRequest === submittedSyncRequest) currentSyncRequest = null;
      if (!ownsCurrentModal) return;
      confirmBtn.disabled = false;
      confirmBtn.querySelector('.ndi-sync-row-idle').classList.remove('d-none');
      confirmBtn.querySelector('.ndi-sync-row-loading').classList.add('d-none');
      errorDiv.textContent = error.message || 'Sync failed';
      errorDiv.classList.remove('d-none');
    });
  });
}());
