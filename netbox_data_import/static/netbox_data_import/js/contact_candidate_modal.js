/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

(function () {
  var modal = document.getElementById('contactCandidateModal');
  var form = document.getElementById('contactCandidateForm');
  if (!modal || !form) return;

  var candidateValues = JSON.parse(
    (document.getElementById('ndi-candidate-values-by-row') || {textContent: '{}'}).textContent
  );
  var contactSuggestions = JSON.parse(
    (document.getElementById('ndi-contact-suggestions-by-row') || {textContent: '{}'}).textContent
  );
  var noContact = document.getElementById('contactCandidateNone');
  var existingContact = document.getElementById('contactCandidateExisting');
  var contactId = document.getElementById('contactCandidateContactId');
  var suggestionMessage = document.getElementById('contactCandidateSuggestion');
  var selects = {
    name: document.getElementById('contactCandidateName'),
    email: document.getElementById('contactCandidateEmail'),
    phone: document.getElementById('contactCandidatePhone'),
  };
  var valueInputs = {
    name: document.getElementById('contactCandidateNameValue'),
    email: document.getElementById('contactCandidateEmailValue'),
    phone: document.getElementById('contactCandidatePhoneValue'),
  };

  function contactOption(contact) {
    return {
      id: String(contact.id),
      name: contact.name || '',
      email: contact.email || '',
      phone: contact.phone || '',
      label: [contact.name, contact.email, contact.phone].filter(Boolean).join(' · '),
    };
  }

  function clearSelectedContact() {
    contactId.value = '';
    if (existingContact.tomselect && existingContact.tomselect.getValue()) {
      existingContact.tomselect.clear(true);
    }
  }

  function applyExistingContact(value) {
    if (!value || !existingContact.tomselect) {
      contactId.value = '';
      return;
    }
    var contact = existingContact.tomselect.options[value];
    if (!contact) return;
    contactId.value = String(contact.id);
    for (var fieldName in valueInputs) {
      valueInputs[fieldName].value = contact[fieldName] || '';
      selects[fieldName].tomselect?.clear(true);
    }
  }

  if (window.TomSelect) {
    if (existingContact.tomselect) existingContact.tomselect.destroy();
    new window.TomSelect(existingContact, {
      valueField: 'id',
      labelField: 'label',
      searchField: ['name', 'email', 'phone'],
      plugins: ['remove_button'],
      maxItems: 1,
      load: function (query, callback) {
        if (query.length < 2) {
          callback();
          return;
        }
        var separator = form.dataset.contactLookupUrl.includes('?') ? '&' : '?';
        fetch(form.dataset.contactLookupUrl + separator + 'q=' + encodeURIComponent(query), {
          headers: {'Accept': 'application/json'},
        })
          .then(function (response) { return response.json(); })
          .then(function (data) { callback((data.results || []).map(contactOption)); })
          .catch(function () { callback(); });
      },
      onChange: applyExistingContact,
    });
  }

  function toggleContactFields() {
    var disabled = noContact.checked;
    var lookupField = form.dataset.contactLookupField;
    existingContact.disabled = disabled;
    if (existingContact.tomselect) {
      if (disabled) existingContact.tomselect.disable();
      else existingContact.tomselect.enable();
    }
    for (var fieldName in selects) {
      var select = selects[fieldName];
      select.disabled = disabled;
      valueInputs[fieldName].disabled = disabled;
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
    var selectedValues = resolvedFields.contact_field_values || {};
    var suggestion = contactSuggestions[button.dataset.rowNumber];
    var proposeSuggestion = !existing && suggestion;

    document.getElementById('contactCandidateSourceId').value = sourceId;
    document.getElementById('contactCandidateOriginalValue').value = JSON.stringify(rowCandidates);
    contactId.value = resolvedFields.contact_id || '';
    if (existingContact.tomselect) {
      existingContact.tomselect.clear(true);
      existingContact.tomselect.clearOptions();
      if (suggestion) existingContact.tomselect.addOption(contactOption(suggestion));
      if (suggestion && String(suggestion.id) === String(resolvedFields.contact_id || '')) {
        existingContact.tomselect.setValue(String(suggestion.id), true);
      }
      existingContact.tomselect.refreshOptions(false);
    }
    if (suggestionMessage) {
      suggestionMessage.classList.toggle('d-none', !suggestion);
      suggestionMessage.textContent = suggestion
        ? 'A NetBox Contact with this row\'s configured identity already exists. It is proposed below for reuse.'
        : '';
    }
    for (var fieldName in selects) {
      populateSelect(selects[fieldName], rowCandidates, selectedSources[fieldName] || '');
      valueInputs[fieldName].value = selectedValues[fieldName] || '';
      valueInputs[fieldName].setCustomValidity('');
    }
    noContact.checked = resolvedFields.contact_resolution_applied === true
      && Object.keys(selectedSources).length === 0;
    toggleContactFields();
    if (proposeSuggestion && existingContact.tomselect) {
      existingContact.tomselect.setValue(String(suggestion.id), true);
      applyExistingContact(String(suggestion.id));
    }
  });

  noContact.addEventListener('change', toggleContactFields);
  for (var fieldName in selects) {
    (function (name) {
      selects[name].addEventListener('change', function () {
        if (selects[name].value) valueInputs[name].value = '';
        clearSelectedContact();
      });
      valueInputs[name].addEventListener('input', function () {
        if (valueInputs[name].value.trim() && selects[name].tomselect) {
          selects[name].tomselect.clear(true);
        }
        clearSelectedContact();
        valueInputs[name].setCustomValidity('');
      });
    }(fieldName));
  }

  form.addEventListener('submit', function (event) {
    var fieldSources = {};
    var fieldValues = {};
    if (!noContact.checked) {
      for (var fieldName in selects) {
        var literal = valueInputs[fieldName].value.trim();
        if (literal) fieldValues[fieldName] = literal;
        else if (selects[fieldName].value) fieldSources[fieldName] = selects[fieldName].value;
      }
      var lookupField = form.dataset.contactLookupField;
      for (var requiredField of ['name', lookupField]) {
        if (!fieldSources[requiredField] && !fieldValues[requiredField]) {
          valueInputs[requiredField].setCustomValidity('Select a source column or enter a value.');
          valueInputs[requiredField].reportValidity();
          event.preventDefault();
          return;
        }
      }
    }
    document.getElementById('contactCandidateResolvedFields').value = JSON.stringify({
      contact_resolution_applied: true,
      contact_field_sources: fieldSources,
      contact_field_values: fieldValues,
      contact_id: !noContact.checked && contactId.value ? Number(contactId.value) : null,
    });
  });
}());
