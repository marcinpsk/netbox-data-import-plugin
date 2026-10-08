/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* Termination cards as `_termination_card.html` renders them, with the same htmx attributes, so the
 * scripts run against the markup the server sends. */
export const READ_URL = '/plugins/data-import/trace-workspace/proposals/';
export const ACTION_URLS = {
  request: '/plugins/data-import/trace-workspace/proposals/request/',
  cancel: '/plugins/data-import/trace-workspace/proposals/cancel/',
  accept: '/plugins/data-import/trace-workspace/proposals/accept/',
  reject: '/plugins/data-import/trace-workspace/proposals/reject/',
};
export const ASK_ALL_URL = '/plugins/data-import/trace-workspace/proposals/request-all/';
export const CABLE_POLICY_URL = '/plugins/data-import/trace-workspace/cable-policy/';

export function claimFields(revision = 4) {
  return [
    ['preview_token', 'token-1'], ['preview_revision', String(revision)],
    ['preview_document', '11'], ['preview_profile', '3'],
  ];
}

function claimInputs(revision) {
  return claimFields(revision).map(([name, value]) => `<input type="hidden" name="${name}" value="${value}">`).join('');
}

/* The disabled reason of each action in each card state, as ProposalPresentation offers them. */
const STATES = {
  open: {request: '', cancel: 'There is no active proposal.'},
  pending: {
    request: 'An active proposal already exists for this field.', cancel: '',
    accept: 'Wait for a completed proposal.', reject: 'Wait for a completed proposal.',
  },
  completed: {request: '', cancel: 'There is no active proposal.', accept: '', reject: ''},
};
const LABELS = {request: 'Ask AI', cancel: 'Cancel', accept: 'Accept', reject: 'Reject'};

function actionForm(id, field, key, reason, revision, proposal) {
  const target = key === 'accept'
    ? 'hx-target="#page-content" hx-select="#page-content" hx-swap="outerHTML" hx-push-url="true"'
    : 'hx-target="closest [data-proposal-field]" hx-swap="outerHTML"';
  const style = key === 'request' || key === 'accept' ? 'btn-primary' : 'btn-secondary';
  return `
    <form class="ndi-trace-action" method="post" action="${ACTION_URLS[key]}" hx-post="${ACTION_URLS[key]}" ${target}
          hx-sync="closest [data-proposal-field]:replace" hx-disabled-elt="#${id} [data-proposal-action]">
      <input type="hidden" name="csrfmiddlewaretoken" value="fixture-token">${claimInputs(revision)}
      <input type="hidden" name="trace" value="trace-1"><input type="hidden" name="field_key" value="${field}">
      ${proposal ? `<input type="hidden" name="proposal_id" value="${proposal}">` : ''}
      <button type="submit" class="btn btn-sm ${style}" data-proposal-action="${key}" ${reason ? 'disabled' : ''}><span
        class="spinner-border spinner-border-sm ndi-busy" aria-hidden="true"></span>${LABELS[key]}</button>
      <div class="ndi-trace-reason" data-proposal-reason="${key}" ${reason ? '' : 'hidden'}>${reason}</div>
    </form>`;
}

/* One card for `field` in `state`: open, pending or completed. */
export function card(field, state = 'open', {revision = 4, proposal = 7} = {}) {
  const id = `proposalCard${field}`;
  const reasons = STATES[state];
  const query = [['field_key', field], ['trace', 'trace-1'], ...claimFields(revision)];
  const read = `${READ_URL}?${query.map(pair => pair.join('=')).join('&amp;')}`;
  const poll = state === 'pending' ? `hx-get="${read}" hx-trigger="every 3s" hx-swap="outerHTML" hx-sync="this:abort"` : '';
  const display = state === 'open' ? '' : `
    <div class="ndi-proposal-card mt-2" data-proposal-display>
      <span class="badge" data-proposal-badge>${state === 'pending' ? 'Queued' : 'Proposal - not applied'}</span>
      ${state === 'pending' ? '<span data-proposal-progress>Waiting for the backend...</span>' : ''}
      <div class="ndi-trace-actions">
        ${['accept', 'reject'].map(key => actionForm(id, field, key, reasons[key], revision, proposal)).join('')}
      </div>
    </div>`;
  const fieldActions = ['request', 'cancel']
    .map(key => actionForm(id, field, key, reasons[key], revision, state === 'open' ? null : proposal)).join('');
  return `
    <li class="card ndi-proposal-card mb-3" id="${id}" data-proposal-field="${field}" data-proposal-read="${read}" ${poll}>
      <strong>${field}</strong>
      <span class="badge" data-proposal-state>${state === 'open' ? 'unresolved' : 'proposed'}</span>
      ${display}
      <div class="ndi-trace-actions mt-2" data-proposal-field-actions>${fieldActions}</div>
      <div class="alert alert-danger" role="alert" data-proposal-error hidden></div>
    </li>`;
}

/* The workspace content that the page and every swap of #page-content carry: the claim, the strip, the cards. */
export function workspace(cards, {revision = 4, note = '', active = 0, countedAt = 100} = {}) {
  return `
    <div id="page-content">
      <div class="ndi-trace-workspace">
        <form id="ndi-preview-claim" hidden>${claimInputs(revision)}</form>
        <output id="ndi-revision">${revision}</output>
        <div id="ndiActiveProposals" data-counted-at="${countedAt}">${active}</div>
        <p id="ndi-note">${note}</p>
        <form class="ndi-trace-action" method="post" action="${ASK_ALL_URL}" hx-post="${ASK_ALL_URL}"
              hx-target="#page-content" hx-select="#page-content" hx-swap="outerHTML" hx-push-url="true"
              hx-disabled-elt="find button">
          <input type="hidden" name="csrfmiddlewaretoken" value="fixture-token">${claimInputs(revision)}
          <input type="hidden" name="trace" value="trace-1">
          <button type="submit" class="btn btn-primary" data-proposal-ask-all><span
            class="spinner-border spinner-border-sm ndi-busy" aria-hidden="true"></span>Ask AI for all</button>
        </form>
        <form method="post" action="${CABLE_POLICY_URL}" hx-post="${CABLE_POLICY_URL}"
              hx-target="#page-content" hx-select="#page-content" hx-swap="outerHTML" hx-push-url="true">
          <input type="hidden" name="csrfmiddlewaretoken" value="fixture-token">${claimInputs(revision)}
          <button type="submit" class="btn btn-sm btn-primary" data-cable-policy-save>Save</button>
        </form>
        <ul class="list-unstyled">${cards.join('')}</ul>
      </div>
    </div>`;
}

/* A card answer as `_proposal_card_answer.html` renders it: the card, and the strip count out of band. */
export function answer(field, state, active, countedAt) {
  const count = `<div class="h2 mb-0" id="ndiActiveProposals" data-counted-at="${countedAt}" hx-swap-oob="true">${active}</div>`;
  return `${card(field, state)}\n${count}`;
}
