/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

(function () {
  var modal = document.getElementById('contactCandidateModal');
  var form = document.getElementById('contactCandidateForm');
  if (!modal || !form) return;

  var candidateValues = JSON.parse(
    (document.getElementById('ndi-candidate-values-by-row') || {textContent: '{}'}).textContent
  );
  var noContact = document.getElementById('contactCandidateNone');
  var selects = {
    name: document.getElementById('contactCandidateName'),
    email: document.getElementById('contactCandidateEmail'),
    phone: document.getElementById('contactCandidatePhone'),
  };

  function toggleContactFields() {
    var disabled = noContact.checked;
    var lookupField = form.dataset.contactLookupField;
    for (var fieldName in selects) {
      var select = selects[fieldName];
      select.disabled = disabled;
      select.required = !disabled && (fieldName === 'name' || fieldName === lookupField);
      if (select.tomselect) {
        if (disabled) {
          select.tomselect.disable();
        } else {
          select.tomselect.enable();
        }
      }
    }
    document.getElementById('contactCandidateFields').classList.toggle('opacity-50', disabled);
  }

  function populateSelect(select, candidates, selectedSource) {
    select.textContent = '';
    var emptyOption = document.createElement('option');
    emptyOption.value = '';
    emptyOption.textContent = '— not supplied —';
    select.appendChild(emptyOption);
    for (var sourceColumn in candidates) {
      var option = document.createElement('option');
      option.value = sourceColumn;
      option.textContent = sourceColumn + ': ' + candidates[sourceColumn];
      option.selected = sourceColumn === selectedSource;
      select.appendChild(option);
    }
    if (select.tomselect) {
      select.tomselect.clearOptions(function () { return false; });
      select.tomselect.sync();
    }
  }

  modal.addEventListener('show.bs.modal', function (event) {
    var button = event.relatedTarget;
    if (!button) return;
    var sourceId = button.dataset.sourceId || '';
    var rowCandidates = (candidateValues[button.dataset.rowNumber] || {}).contact || {};
    var existing = (EXISTING_RESOLUTIONS[sourceId] || {})['candidate:contact'];
    var resolvedFields = existing ? (existing.resolved_fields || {}) : {};
    var selectedSources = resolvedFields.contact_field_sources || {};

    document.getElementById('contactCandidateSourceId').value = sourceId;
    document.getElementById('contactCandidateOriginalValue').value = JSON.stringify(rowCandidates);
    for (var fieldName in selects) {
      populateSelect(selects[fieldName], rowCandidates, selectedSources[fieldName] || '');
    }
    noContact.checked = resolvedFields.contact_resolution_applied === true
      && Object.keys(selectedSources).length === 0;
    toggleContactFields();
  });

  noContact.addEventListener('change', toggleContactFields);
  form.addEventListener('submit', function () {
    var fieldSources = {};
    if (!noContact.checked) {
      for (var fieldName in selects) {
        if (selects[fieldName].value) fieldSources[fieldName] = selects[fieldName].value;
      }
    }
    document.getElementById('contactCandidateResolvedFields').value = JSON.stringify({
      contact_resolution_applied: true,
      contact_field_sources: fieldSources,
    });
  });
}());
