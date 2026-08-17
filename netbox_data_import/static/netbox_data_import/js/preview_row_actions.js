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
        // An HTML error page or login redirect would surface as a JSON parse error.
        return response.json().catch(function () {
          throw new Error('The server returned an unexpected response (HTTP ' + response.status + ').');
        }).then(function (payload) {
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

  /* Recalculation reloads the whole preview and can take a while, so the page reports that it
   * was pressed and refuses a second press until the new page arrives. The page holds more
   * than one link to the same recalculation, so pressing one latches them all. */
  document.addEventListener('click', function (event) {
    var recalculate = event.target.closest('.ndi-recalculate-preview');
    if (!recalculate) return;
    // A modified click opens a second tab and leaves this page, and its links, as they are.
    // It runs before the latch so a latched link still opens a second tab.
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (recalculate.dataset.ndiRecalculating === 'true') {
      event.preventDefault();
      return;
    }
    document.querySelectorAll('.ndi-recalculate-preview').forEach(function (link) {
      link.dataset.ndiRecalculating = 'true';
      link.classList.add('disabled');
      link.setAttribute('aria-busy', 'true');
      link.setAttribute('aria-disabled', 'true');
    });
    recalculate.innerHTML = '<i class="mdi mdi-loading mdi-spin"></i> Recalculating...';
  });

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
