/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

(function () {
  function csrfToken() {
    return document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  }

  function setPending(button, label) {
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<i class="mdi mdi-loading mdi-spin"></i> ' + label;
  }

  function restore(button, message) {
    button.disabled = false;
    button.innerHTML = button.dataset.originalHtml || button.textContent;
    button.title = message;
    button.classList.add('btn-danger');
  }

  function replacePreviewRow(rowNumber, rowHtml) {
    var oldRow = document.getElementById('row-' + rowNumber);
    var oldDetail = document.getElementById('diff-' + rowNumber);
    if (!oldRow) throw new Error('The preview row is no longer present.');
    var expanded = oldDetail?.classList.contains('show') || false;

    var table = document.createElement('table');
    table.innerHTML = '<tbody>' + rowHtml + '</tbody>';
    var newRow = table.querySelector('#row-' + CSS.escape(String(rowNumber)));
    var newDetail = table.querySelector('#diff-' + CSS.escape(String(rowNumber)));
    if (!newRow) throw new Error('The refreshed preview row is invalid.');

    oldRow.replaceWith(newRow);
    if (oldDetail) {
      if (newDetail) oldDetail.replaceWith(newDetail);
      else oldDetail.remove();
    } else if (newDetail) {
      newRow.after(newDetail);
    }
    if (expanded && newDetail) {
      newDetail.classList.add('show');
      document.querySelectorAll('[data-diff-target="diff-' + CSS.escape(String(rowNumber)) + '"]')
        .forEach(function (toggle) { toggle.setAttribute('aria-expanded', 'true'); });
    }
  }

  function postAction(url, body, button, pendingLabel, placementError) {
    setPending(button, pendingLabel);
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
        replacePreviewRow(payload.row_number, payload.row_html);
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
        device_id: button.dataset.deviceId,
        rack_name: button.dataset.rackName,
        u_position: button.dataset.uPosition || '',
        face: button.dataset.face || '',
        row_number: button.dataset.rowId,
      });
      url = button.dataset.actionUrl || '/plugins/data-import/sync-placement/';
      postAction(url, body, button, 'Syncing...', true);
      return;
    }

    body = new URLSearchParams({
      device_id: button.dataset.deviceId,
      field: button.dataset.field,
      value: button.dataset.value,
      profile_id: button.dataset.profileId || '',
      row_number: button.dataset.rowId,
    });
    url = button.dataset.actionUrl || '/plugins/data-import/sync-device-field/';
    postAction(url, body, button, 'Updating...', false);
  }, true);
}());
