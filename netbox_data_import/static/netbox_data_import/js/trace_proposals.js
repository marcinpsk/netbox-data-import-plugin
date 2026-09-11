/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

(function () {
  if (window.ndiTraceProposals) {
    window.ndiTraceProposals.init();
    return;
  }

  var cards = new Map();

  function node(card, name) {
    return card.querySelector('[data-proposal-' + name + ']');
  }

  function error(card, message) {
    var target = node(card, 'error');
    target.textContent = message;
    target.hidden = !message;
  }

  function stop(card) {
    var state = cards.get(card);
    if (!state) return;
    clearTimeout(state.timer);
    if (state.controller) state.controller.abort();
    cards.delete(card);
  }

  function schedule(card) {
    var state = cards.get(card);
    clearTimeout(state.timer);
    if (!card.isConnected || !state.payload.presentation.pending) return;
    state.timer = setTimeout(function () {
      if (!card.isConnected) { stop(card); return; }
      refresh(card);
    }, 3000);
  }

  function render(card, payload) {
    var state = cards.get(card);
    state.payload = payload;
    var display = payload.presentation;
    var badge = node(card, 'state');
    badge.textContent = display.field_state;
    badge.className = 'badge ' + badge.dataset.proposalStatePrefix + display.state_style;
    node(card, 'display').hidden = !payload.proposal;
    ['badge', 'candidate', 'explanation'].forEach(function (name) {
      node(card, name).textContent = display[name];
    });
    node(card, 'progress').hidden = !display.pending;
    node(card, 'failure').textContent = display.failure
      ? display.failure + ' (' + display.failure_code + ')' : '';
    node(card, 'attempts').textContent = 'Backend attempts: ' + display.attempt_count;
    var metadata = node(card, 'metadata');
    metadata.replaceChildren();
    display.metadata.forEach(function (item) {
      var row = document.createElement('li');
      row.textContent = item.label + ': ' + item.value;
      metadata.appendChild(row);
    });
    display.actions.forEach(function (action) {
      var button = card.querySelector('[data-proposal-action="' + action.key + '"]');
      button.textContent = action.label;
      button.disabled = state.busy || Boolean(action.reason);
      var reason = card.querySelector('[data-proposal-reason="' + action.key + '"]');
      reason.textContent = action.reason;
      reason.hidden = !action.reason;
    });
    var history = node(card, 'history');
    history.replaceChildren();
    payload.history_display.forEach(function (attempt) {
      var row = document.createElement('li');
      row.textContent = '#' + attempt.id + ' · ' + attempt.created + ' · ' + attempt.status + ' · '
        + attempt.outcome + (attempt.decision ? ' · ' + attempt.decision : '')
        + (attempt.failure ? ' · ' + attempt.failure : '');
      history.appendChild(row);
    });
    node(card, 'empty-history').hidden = payload.history_display.length > 0;
    schedule(card);
  }

  async function readResponse(response) {
    var payload;
    try { payload = await response.json(); }
    catch (_) { throw new Error('The proposal response could not be read. Reload the workspace.'); }
    if (!response.ok || !payload.ok) throw new Error(payload.error || 'The proposal request was refused.');
    return payload;
  }

  async function refresh(card) {
    var state = cards.get(card);
    if (!state || !card.isConnected) return;
    var generation = ++state.generation;
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    var url = new URL(card.dataset.proposalUrl, document.baseURI);
    url.searchParams.set('field_key', card.dataset.proposalField);
    url.searchParams.set('preview_revision', card.dataset.previewRevision);
    try {
      var payload = await fetch(url, {
        headers: {Accept: 'application/json'}, credentials: 'same-origin', signal: state.controller.signal,
      }).then(readResponse);
      if (!card.isConnected || generation !== state.generation) return;
      error(card, "");
      render(card, payload);
    } catch (failure) {
      if (!card.isConnected || generation !== state.generation || failure.name === 'AbortError') return;
      error(card, failure.message);
      schedule(card);
    }
  }

  function replan(card) {
    var reread = document.getElementById('traceWorkspaceReread');
    if (!reread || reread.disabled) {
      error(card, 'The resolution was saved. Re-read the workspace when the active sync finishes.');
      return;
    }
    reread.form.requestSubmit(reread);
  }

  async function act(card, key) {
    var state = cards.get(card);
    var action = state.payload.presentation.actions.find(function (item) { return item.key === key; });
    if (state.busy || !action || action.reason) return;
    state.busy = true;
    state.generation += 1;
    clearTimeout(state.timer);
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    error(card, '');
    card.querySelectorAll('[data-proposal-action]').forEach(function (button) { button.disabled = true; });
    var form = document.getElementById('traceTerminationForm');
    var data = new FormData();
    data.set('csrfmiddlewaretoken', form.elements.namedItem('csrfmiddlewaretoken').value);
    data.set('preview_revision', card.dataset.previewRevision);
    data.set('field_key', card.dataset.proposalField);
    if (state.payload.proposal) data.set('proposal_id', state.payload.proposal.id);
    try {
      var payload = await fetch(action.url, {
        method: 'POST', body: data, headers: {Accept: 'application/json'},
        credentials: 'same-origin', signal: state.controller.signal,
      }).then(readResponse);
      if (!card.isConnected) return;
      state.busy = false;
      render(card, state.payload);
      await refresh(card);
      if (card.isConnected && payload.preview_state) replan(card);
    } catch (failure) {
      if (!card.isConnected || failure.name === 'AbortError') return;
      state.busy = false;
      render(card, state.payload);
      await refresh(card);
      if (card.isConnected) error(card, failure.message);
    }
  }

  function init() {
    cards.forEach(function (_, card) { if (!card.isConnected) stop(card); });
    var source = document.getElementById('traceProposalFields');
    if (!source) return;
    var fields = JSON.parse(source.textContent);
    document.querySelectorAll('[data-proposal-field]').forEach(function (card) {
      if (cards.has(card)) return;
      cards.set(card, {generation: 0, busy: false, timer: null, controller: null});
      render(card, fields[card.dataset.proposalField]);
    });
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-proposal-action]');
    if (!button || button.disabled) return;
    var card = button.closest('[data-proposal-field]');
    if (cards.has(card)) act(card, button.dataset.proposalAction);
  });
  document.addEventListener('htmx:load', init);
  new MutationObserver(function () {
    cards.forEach(function (_, card) { if (!card.isConnected) stop(card); });
  }).observe(document.documentElement, {childList: true, subtree: true});
  window.ndiTraceProposals = {init: init};
  init();
}());
