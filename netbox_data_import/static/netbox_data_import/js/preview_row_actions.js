/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

(function () {
  function csrfToken() {
    return document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  }

  function previewRevision() {
    return document.getElementById('ndi-preview-revision')?.value || '';
  }

  function setPending(button, label) {
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.classList.remove('btn-danger');
    button.title = '';
    var container = button.closest('form') || button.parentElement;
    container?.querySelector('.ndi-row-action-error')?.remove();
    button.innerHTML = '<i class="mdi mdi-loading mdi-spin"></i> ' + label;
  }

  function restore(button, message) {
    button.disabled = false;
    button.innerHTML = button.dataset.originalHtml || button.textContent;
    button.title = message;
    button.classList.add('btn-danger');
    var container = button.closest('form') || button.parentElement;
    if (container) {
      var error = document.createElement('div');
      error.className = 'ndi-row-action-error small text-danger mt-2';
      error.setAttribute('role', 'alert');
      error.textContent = message;
      container.appendChild(error);
    }
  }

  function markSaved(button, message) {
    button.disabled = true;
    button.innerHTML = '<i class="mdi mdi-check"></i> Saved';
    button.title = message || 'Saved. Recalculate the preview to refresh this row.';
    var staleNotice = document.getElementById('ndi-preview-stale');
    if (staleNotice) staleNotice.hidden = false;
    var runImport = document.getElementById('ndi-run-import');
    if (runImport) {
      runImport.disabled = true;
      runImport.title = 'Recalculate the preview before importing.';
    }
  }

  function postAction(url, body, button, pendingLabel, placementError) {
    setPending(button, pendingLabel);
    body.set('preview_revision', previewRevision());
    return fetch(url, {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
        'X-CSRFToken': csrfToken(),
      },
      body: body,
    })
      .then(function (response) {
        return response.json().then(function (payload) {
          if (!response.ok || !payload.ok) {
            throw new Error(payload.error || 'The preview action failed.');
          }
          return payload;
        });
      })
      .then(function (payload) {
        if (payload.preview_state !== 'recalculation_required') {
          throw new Error('The preview action returned an invalid state.');
        }
        markSaved(button, payload.message);
        return payload;
      })
      .catch(function (error) {
        restore(button, error.message);
        if (placementError) window.alert('Placement sync failed: ' + error.message);
      });
  }

  document.addEventListener('submit', function (event) {
    var form = event.target.closest('.ndi-field-review-form');
    if (!form) return;
    event.preventDefault();
    event.stopPropagation();
    var button = form.querySelector('button[type=submit]');
    postAction(form.action, new FormData(form), button, 'Updating...', false);
  }, true);

  document.addEventListener('click', function (event) {
    var placement = event.target.closest('.ndi-sync-placement-btn');
    var field = event.target.closest('.ndi-sync-btn');
    if (!placement && !field) return;
    event.preventDefault();
    event.stopPropagation();

    var button = placement || field;
    var body;
    var url;
    if (placement) {
      body = new URLSearchParams({
        row_number: button.dataset.rowId,
      });
      url = button.dataset.actionUrl || '/plugins/data-import/sync-placement/';
      postAction(url, body, button, 'Syncing...', true);
      return;
    }

    body = new URLSearchParams({
      field: button.dataset.field,
      row_number: button.dataset.rowId,
    });
    url = button.dataset.actionUrl || '/plugins/data-import/sync-device-field/';
    postAction(url, body, button, 'Updating...', false);
  }, true);
}());
