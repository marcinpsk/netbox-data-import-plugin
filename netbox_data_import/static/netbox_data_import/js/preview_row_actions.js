/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* A preview command replans the preview and advances its revision on the server (ADR 0004), so
 * every successful command leaves this page stale and the page loads again. */
(function () {
  function csrfToken() {
    return document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  }

  /* The claim this page holds is spent once a reload starts, so the latch lives on the claim. */
  function reloadPreview() {
    var claim = document.getElementById('ndi-preview-claim');
    if (claim) {
      if (claim.dataset.ndiReloading === 'true') return;
      claim.dataset.ndiReloading = 'true';
    }
    if (typeof window.ndiRememberPreviewView === 'function') window.ndiRememberPreviewView();
    window.location.reload();
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

  /* The one place that states the row command contract, so the modals and the row buttons
   * cannot drift over the envelope or the claim. */
  function requestAction(url, body) {
    return Promise.resolve().then(function () {
      return fetch(url, {
        method: 'POST',
        headers: {
          'Accept': 'application/json',
          'X-CSRFToken': csrfToken(),
        },
        body: window.ndiPreviewClaim(body),
      });
    })
      .then(function (response) {
        // An HTML error page or login redirect would surface as a JSON parse error.
        return response.json().catch(function () {
          throw new Error('The server returned an unexpected response (HTTP ' + response.status + ').');
        }).then(function (payload) {
          if (!response.ok || !payload.ok) {
            throw new Error(payload.error || 'The preview action failed.');
          }
          if (payload.preview_state !== 'replanned') {
            throw new Error('The preview action returned an invalid state.');
          }
          return payload;
        });
      });
  }

  window.ndiPostPreviewAction = requestAction;
  window.ndiReloadPreview = reloadPreview;

  function postAction(url, body, button, pendingLabel, placementError) {
    setPending(button, pendingLabel);
    return requestAction(url, body)
      .then(reloadPreview)
      .catch(function (error) {
        restore(button, error.message);
        if (placementError) window.alert('Placement sync failed: ' + error.message);
      });
  }

  document.addEventListener('submit', function (event) {
    var form = event.target.closest('.ndi-field-review-form, .ndi-deferred-preview-form');
    if (!form) return;
    event.preventDefault();
    event.stopPropagation();
    var button = event.submitter || form.querySelector('button[type=submit]');
    // A control named `action` shadows the form property of the same name, so read the attribute.
    postAction(form.getAttribute('action'), new FormData(form), button, 'Updating...', false);
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
