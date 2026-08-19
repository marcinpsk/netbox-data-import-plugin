/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The modal asks what each value in the row is, not which column fills each Contact field.
 * A row carries a handful of values and the server proposes a role for the ones it recognizes,
 * so the common case is a glance and a save. */
(function () {
  var modal = document.getElementById('contactCandidateModal');
  var form = document.getElementById('contactCandidateForm');
  if (!modal || !form) return;

  var ROLES = [
    ['name', 'Name'],
    ['email', 'Email'],
    ['phone', 'Phone'],
    ['', 'Not used'],
  ];
  var ROLE_LABELS = {name: 'Name', email: 'Email', phone: 'Phone'};

  function readJson(id) {
    var node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : {};
  }

  var candidateValues = readJson('ndi-candidate-values-by-row');
  var contactSuggestions = readJson('ndi-contact-suggestions-by-row');
  var roleSuggestions = readJson('ndi-contact-role-suggestions-by-row');

  var noContact = document.getElementById('contactCandidateNone');
  var existingContact = document.getElementById('contactCandidateExisting');
  var existingWrap = document.getElementById('contactCandidateExistingWrap');
  var linkExisting = document.getElementById('contactCandidateLinkExisting');
  var contactId = document.getElementById('contactCandidateContactId');
  var suggestionMessage = document.getElementById('contactCandidateSuggestion');
  var valueRows = document.getElementById('contactCandidateValueRows');
  var editPanel = document.getElementById('contactCandidateEdit');
  var editToggle = document.getElementById('contactCandidateEditToggle');
  var addValue = document.getElementById('contactCandidateAddValue');
  var provenance = document.getElementById('contactCandidateProvenance');
  var summary = {
    name: document.getElementById('contactCandidateSummaryName'),
    email: document.getElementById('contactCandidateSummaryEmail'),
    phone: document.getElementById('contactCandidateSummaryPhone'),
  };

  /* NetBox enhances every plain <select> with its own Tom Select instance and does not
   * export the library, so this picker adopts that instance instead of creating one. */
  function picker() {
    var instance = existingContact.tomselect;
    if (!instance || instance.ndiContactSearch) return instance;
    instance.ndiContactSearch = true;
    // Deliberately uncached: a Contact created while this page is open must be findable.
    instance.settings.load = function (query, callback) {
      var lookupUrl = form.dataset.contactLookupUrl;
      if (query.length < 2 || !lookupUrl) {
        callback();
        return;
      }
      var separator = lookupUrl.includes('?') ? '&' : '?';
      fetch(lookupUrl + separator + 'q=' + encodeURIComponent(query), {
        headers: {'Accept': 'application/json'},
      })
        .then(function (response) { return response.json(); })
        .then(function (data) { callback((data.results || []).map(contactOption)); })
        .catch(function () { callback(); });
    };
    instance.on('change', applyExistingContact);
    return instance;
  }

  function contactOption(contact) {
    var instance = existingContact.tomselect;
    var option = {
      id: String(contact.id),
      name: contact.name || '',
      email: contact.email || '',
      phone: contact.phone || '',
    };
    option[instance.settings.valueField] = String(contact.id);
    option[instance.settings.labelField] = [contact.name, contact.email, contact.phone]
      .filter(Boolean)
      .join(' · ');
    return option;
  }

  function roleSelect(selected) {
    var select = document.createElement('select');
    select.className = 'form-select form-select-sm ndi-contact-role';
    select.setAttribute('aria-label', 'Use this value as');
    ROLES.forEach(function (role) {
      var option = document.createElement('option');
      option.value = role[0];
      option.textContent = role[1];
      option.selected = role[0] === selected;
      select.appendChild(option);
    });
    select.addEventListener('change', function () {
      releaseRole(select);
      clearSelectedContact();
      refreshSummary();
    });
    return select;
  }

  /* A Contact field takes one value, so claiming a role releases whichever row held it. */
  function releaseRole(changed) {
    if (!changed.value) return;
    valueRows.querySelectorAll('.ndi-contact-role').forEach(function (other) {
      if (other !== changed && other.value === changed.value) other.value = '';
    });
  }

  function candidateRow(sourceColumn, value, selectedRole) {
    var row = document.createElement('div');
    row.className = 'ndi-contact-value-row';
    row.dataset.sourceColumn = sourceColumn;

    var text = document.createElement('div');
    text.className = 'flex-fill min-w-0';
    var raw = document.createElement('div');
    raw.className = 'text-truncate ndi-contact-raw';
    raw.textContent = value;
    var from = document.createElement('div');
    from.className = 'text-secondary small';
    from.textContent = 'from “' + sourceColumn + '”';
    text.appendChild(raw);
    text.appendChild(from);

    row.appendChild(text);
    row.appendChild(roleSelect(selectedRole));
    return row;
  }

  function literalRow(role, value) {
    var row = document.createElement('div');
    row.className = 'ndi-contact-value-row';
    row.dataset.literal = 'true';

    var wrap = document.createElement('div');
    wrap.className = 'flex-fill min-w-0';
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'form-control form-control-sm ndi-contact-literal';
    input.placeholder = 'Type a value';
    input.setAttribute('aria-label', 'Contact value');
    input.value = value || '';
    input.addEventListener('input', function () {
      clearSelectedContact();
      input.setCustomValidity('');
      refreshSummary();
    });
    wrap.appendChild(input);

    row.appendChild(wrap);
    row.appendChild(roleSelect(role));
    return row;
  }

  /* The selections, read back as {role: value} plus the split the server expects. */
  function readSelection() {
    var sources = {};
    var values = {};
    var resolved = {};
    valueRows.querySelectorAll('.ndi-contact-value-row').forEach(function (row) {
      var role = row.querySelector('.ndi-contact-role').value;
      if (!role) return;
      if (row.dataset.literal) {
        var literal = row.querySelector('.ndi-contact-literal').value.trim();
        if (!literal) return;
        values[role] = literal;
        resolved[role] = literal;
        return;
      }
      sources[role] = row.dataset.sourceColumn;
      resolved[role] = row.querySelector('.ndi-contact-raw').textContent;
    });
    return {sources: sources, values: values, resolved: resolved};
  }

  function refreshSummary() {
    if (noContact.checked) {
      summary.name.hidden = false;
      summary.name.textContent = 'No contact for this row';
      summary.email.hidden = true;
      summary.phone.hidden = true;
      provenance.textContent = '';
      return;
    }
    var selection = readSelection();
    var linked = contactId.value ? (picker() || {}).options : null;
    var linkedContact = linked ? linked[contactId.value] : null;
    var shown = linkedContact
      ? {name: linkedContact.name, email: linkedContact.email, phone: linkedContact.phone}
      : selection.resolved;

    for (var role in summary) {
      summary[role].textContent = shown[role] || '';
      summary[role].hidden = !shown[role];
    }
    if (!shown.name && !shown.email && !shown.phone) {
      summary.name.hidden = false;
      summary.name.textContent = noContact.checked ? 'No contact for this row' : 'Nothing selected yet';
    }

    if (linkedContact) {
      provenance.textContent = 'Linked to an existing NetBox Contact.';
      return;
    }
    var used = Object.keys(selection.sources).map(function (role) {
      return selection.sources[role];
    });
    var unused = [];
    valueRows.querySelectorAll('.ndi-contact-value-row[data-source-column]').forEach(function (row) {
      if (!row.querySelector('.ndi-contact-role').value) unused.push(row.dataset.sourceColumn);
    });
    var parts = [];
    if (used.length) parts.push('Read from ' + used.join(', ') + '.');
    if (unused.length) {
      parts.push(unused.length === 1 ? 'One column is unused: ' + unused[0] + '.'
        : unused.length + ' columns are unused: ' + unused.join(', ') + '.');
    }
    provenance.textContent = parts.join(' ');
  }

  function clearSelectedContact() {
    if (!contactId.value) return;
    contactId.value = '';
    var instance = picker();
    if (instance && instance.getValue()) instance.clear(true);
  }

  function applyExistingContact(value) {
    var instance = existingContact.tomselect;
    if (!value || !instance) {
      contactId.value = '';
      refreshSummary();
      return;
    }
    var contact = instance.options[value];
    if (!contact) return;
    contactId.value = String(contact.id);
    // A linked Contact supplies every field, so no row may also claim one.
    valueRows.querySelectorAll('.ndi-contact-role').forEach(function (select) { select.value = ''; });
    refreshSummary();
  }

  function setExpanded(button, panel, expanded) {
    panel.hidden = !expanded;
    button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  }

  function toggleContactFields() {
    var disabled = noContact.checked;
    var instance = picker();
    existingContact.disabled = disabled;
    if (instance) {
      if (disabled) instance.disable();
      else instance.enable();
    }
    valueRows.querySelectorAll('select, input').forEach(function (control) { control.disabled = disabled; });
    linkExisting.disabled = disabled;
    addValue.disabled = disabled;
    editToggle.disabled = disabled;
    document.getElementById('contactCandidateSummary').classList.toggle('opacity-50', disabled);
    editPanel.classList.toggle('opacity-50', disabled);
    refreshSummary();
  }

  modal.addEventListener('show.bs.modal', function (event) {
    var button = event.relatedTarget;
    if (!button) return;
    var sourceId = button.dataset.sourceId || '';
    var rowNumber = button.dataset.rowNumber;
    var rowCandidates = (candidateValues[rowNumber] || {}).contact || {};
    var resolutions = window.EXISTING_RESOLUTIONS || {};
    var existing = (resolutions[sourceId] || {})['candidate:contact'];
    var resolvedFields = existing ? (existing.resolved_fields || {}) : {};
    var savedSources = resolvedFields.contact_field_sources || {};
    var savedValues = resolvedFields.contact_field_values || {};
    var suggestion = contactSuggestions[rowNumber];
    var proposed = roleSuggestions[rowNumber] || {};

    document.getElementById('contactCandidateSourceId').value = sourceId;
    document.getElementById('contactCandidateOriginalValue').value = JSON.stringify(rowCandidates);
    contactId.value = resolvedFields.contact_id || '';

    // A saved decision wins over the proposal, so re-opening the modal shows what was stored.
    var roleByColumn = {};
    var source = existing ? savedSources : proposed;
    for (var role in source) roleByColumn[source[role]] = role;

    valueRows.textContent = '';
    for (var column in rowCandidates) {
      valueRows.appendChild(candidateRow(column, rowCandidates[column], roleByColumn[column] || ''));
    }
    for (var literalRole in savedValues) {
      valueRows.appendChild(literalRow(literalRole, savedValues[literalRole]));
    }

    var instance = picker();
    if (instance) {
      instance.clear(true);
      instance.clearOptions();
      if (suggestion) instance.addOption(contactOption(suggestion));
      if (suggestion && String(suggestion.id) === String(resolvedFields.contact_id || '')) {
        instance.setValue(String(suggestion.id), true);
      }
      instance.refreshOptions(false);
    }
    if (suggestionMessage) {
      suggestionMessage.classList.toggle('d-none', !suggestion);
      suggestionMessage.textContent = suggestion
        ? 'A NetBox Contact with this row\'s configured identity already exists. Link it below to reuse it.'
        : '';
    }

    noContact.checked = resolvedFields.contact_resolution_applied === true
      && !Object.keys(savedSources).length
      && !Object.keys(savedValues).length
      && !resolvedFields.contact_id;

    setExpanded(editToggle, editPanel, false);
    setExpanded(linkExisting, existingWrap, Boolean(contactId.value || suggestion));
    toggleContactFields();
  });

  editToggle.addEventListener('click', function () {
    setExpanded(editToggle, editPanel, editPanel.hidden);
  });

  linkExisting.addEventListener('click', function () {
    setExpanded(linkExisting, existingWrap, existingWrap.hidden);
    if (!existingWrap.hidden) {
      var instance = picker();
      if (instance) instance.focus();
    }
  });

  addValue.addEventListener('click', function () {
    setExpanded(editToggle, editPanel, true);
    var row = literalRow('', '');
    valueRows.appendChild(row);
    row.querySelector('.ndi-contact-literal').focus();
  });

  noContact.addEventListener('change', toggleContactFields);

  form.addEventListener('submit', function (event) {
    var selection = readSelection();
    if (!noContact.checked && !contactId.value) {
      var required = ['name', form.dataset.contactLookupField];
      for (var index = 0; index < required.length; index++) {
        var role = required[index];
        if (selection.resolved[role]) continue;
        setExpanded(editToggle, editPanel, true);
        var blank = valueRows.querySelector('.ndi-contact-literal') || addBlankFor(role);
        blank.setCustomValidity('Give this row a ' + (ROLE_LABELS[role] || role).toLowerCase() + ', or select no contact.');
        blank.reportValidity();
        event.preventDefault();
        return;
      }
    }
    document.getElementById('contactCandidateResolvedFields').value = JSON.stringify({
      contact_resolution_applied: true,
      contact_field_sources: noContact.checked ? {} : selection.sources,
      contact_field_values: noContact.checked ? {} : selection.values,
      contact_id: !noContact.checked && contactId.value ? Number(contactId.value) : null,
    });
  });

  function addBlankFor(role) {
    var row = literalRow(role, '');
    valueRows.appendChild(row);
    return row.querySelector('.ndi-contact-literal');
  }
}());
