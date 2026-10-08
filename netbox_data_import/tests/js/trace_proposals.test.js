/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* htmx sends the requests and swaps the cards; the script only reads the refusals htmx reports. These
 * tests dispatch the events htmx dispatches, and a fake htmx.ajax swaps in the card a read returns. */
import {readFileSync} from 'node:fs';
import {afterEach, beforeAll, beforeEach, expect, it, vi} from 'vitest';
import {card, workspace} from './trace_proposal_fixture.js';

const source = readFileSync('netbox_data_import/static/netbox_data_import/js/trace_proposals.js', 'utf8');
const cardOf = key => document.querySelector('[data-proposal-field="' + key + '"]');
const errorOf = key => cardOf(key).querySelector('[data-proposal-error]');
const formOf = (key, action) => cardOf(key).querySelector('[data-proposal-action="' + action + '"]').form;
let reads;

function mount(...cards) {
  document.body.innerHTML = workspace(cards.length ? cards : [card('field', 'completed')]);
}

// The read starts after the refused request releases its card, one task later.
const settled = () => new Promise(resolve => { setTimeout(resolve, 0); });

function emit(elt, name, detail = {}) {
  const event = new CustomEvent(name, {bubbles: true, cancelable: true, detail: {elt, ...detail}});
  elt.dispatchEvent(event);
  return event;
}

function refuse(elt, status, body) {
  const responseText = typeof body === 'string' ? body : JSON.stringify(body);
  return emit(elt, 'htmx:responseError', {xhr: {status, responseText}});
}

// The listeners sit on the document, which outlives each test, so the script runs once per file.
beforeAll(() => { window.eval(source); });
beforeEach(() => {
  reads = [];
  // A read answers with the field's card as it stands now, which here holds the active proposal.
  window.htmx = {
    ajax(verb, url, options) {
      reads.push({verb, url, options});
      options.target.outerHTML = card(options.target.dataset.proposalField, 'pending');
      return Promise.resolve();
    },
  };
});
afterEach(() => {
  document.body.replaceChildren();
  delete window.htmx;
  vi.unstubAllGlobals();
});

/* htmx dispatches beforeSwap on the target and names the element that sent the request in requestConfig. */
function swap(elt, target, responseText, responseURL = 'http://preview.test/login/?next=/workspace/') {
  const xhr = {status: 200, responseText, responseURL};
  return emit(target, 'htmx:beforeSwap', {target, shouldSwap: true, xhr, requestConfig: {elt, target}});
}

/* htmx dispatches oobBeforeSwap on the element the out-of-band copy replaces. */
function swapCount(count, countedAt) {
  const fragment = document.createDocumentFragment();
  const copy = document.createElement('div');
  copy.id = 'ndiActiveProposals';
  copy.dataset.countedAt = String(countedAt);
  copy.textContent = String(count);
  fragment.appendChild(copy);
  const target = document.getElementById('ndiActiveProposals');
  return emit(target, 'htmx:oobBeforeSwap', {target, fragment, shouldSwap: true});
}

function hide(hidden) {
  Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
}

it('a refused action reads its card again and shows the refusal in the card that replaced it', async () => {
  mount(card('field', 'open'));
  const before = cardOf('field');

  refuse(formOf('field', 'request'), 409, {ok: false, error: 'This field already has an active Resolution Proposal.'});
  expect(reads).toEqual([]);
  await settled();

  expect(reads).toEqual([{verb: 'GET', url: before.dataset.proposalRead, options: {source: before, target: before, swap: 'outerHTML'}}]);
  expect(cardOf('field')).not.toBe(before);
  expect(cardOf('field').querySelector('[data-proposal-progress]')).not.toBeNull();
  expect(errorOf('field').hidden).toBe(false);
  expect(errorOf('field').textContent).toBe('This field already has an active Resolution Proposal.');
});

it('a stale claim shows the refusal and reads nothing, because the read is refused as well', () => {
  mount();

  refuse(formOf('field', 'accept'), 409, {ok: false, error: 'This preview changed.', code: 'preview_stale'});

  expect(reads).toEqual([]);
  expect(errorOf('field').textContent).toBe('This preview changed.');
  expect(errorOf('field').hidden).toBe(false);
});

it('a refused poll halts the card, so the next interval asks nothing', () => {
  mount(card('field', 'pending'));
  const polling = cardOf('field');

  refuse(polling, 409, {ok: false, error: 'A newer preview replaced this one.', code: 'preview_stale'});

  expect(polling.hasAttribute('data-proposal-halted')).toBe(true);
  expect(errorOf('field').textContent).toBe('A newer preview replaced this one.');
  expect(emit(polling, 'htmx:beforeRequest').defaultPrevented).toBe(true);
  // An action is the operator's own request, so a halted card still sends it.
  expect(emit(formOf('field', 'cancel'), 'htmx:beforeRequest').defaultPrevented).toBe(false);
});

it('a poll that hit a server error says why and keeps polling', () => {
  mount(card('field', 'pending'));

  refuse(cardOf('field'), 502, '<html>Bad Gateway</html>');

  expect(cardOf('field').hasAttribute('data-proposal-halted')).toBe(false);
  expect(errorOf('field').textContent).toBe('The proposal request was refused.');
  expect(emit(cardOf('field'), 'htmx:beforeRequest').defaultPrevented).toBe(false);
});

it('a new action clears the refusal its card shows', () => {
  mount();
  refuse(formOf('field', 'accept'), 409, {ok: false, error: 'Stale.', code: 'preview_stale'});

  emit(formOf('field', 'reject'), 'htmx:beforeRequest');

  expect(errorOf('field').hidden).toBe(true);
  expect(errorOf('field').textContent).toBe('');
});

it('a request that never reached NetBox says so in its card', () => {
  mount(card('field', 'open'), card('other', 'open'));

  emit(formOf('field', 'request'), 'htmx:sendError');

  expect(errorOf('field').textContent).toBe('The proposal request did not reach NetBox. Try again.');
  expect(errorOf('other').hidden).toBe(true);
});

it('a refusal outside every card is left to the page', () => {
  mount();
  const askAll = document.querySelector('[data-proposal-ask-all]').form;

  refuse(askAll, 409, {ok: false, error: 'Stale.'});

  expect(reads).toEqual([]);
  expect(errorOf('field').hidden).toBe(true);
});

it('a second evaluation adds no second listener', async () => {
  mount(card('field', 'open'));
  window.eval(source);

  refuse(formOf('field', 'request'), 400, {ok: false, error: 'No eligible candidates.'});
  await settled();

  expect(reads).toHaveLength(1);
});

it('a login page is not swapped into a polling card; the browser opens it and the card stops', () => {
  const assign = vi.fn();
  vi.stubGlobal('location', {assign, href: 'http://preview.test/workspace/'});
  mount(card('field', 'pending'));
  const polling = cardOf('field');

  const event = swap(polling, polling, '<!doctype html><html><form action="/login/">Log in</form></html>');

  expect(event.detail.shouldSwap).toBe(false);
  expect(assign).toHaveBeenCalledWith('http://preview.test/login/?next=/workspace/');
  expect(polling.hasAttribute('data-proposal-halted')).toBe(true);
  expect(errorOf('field').textContent).toBe('NetBox answered with another page. Opening it.');
});

it('a login page is not swapped into the workspace after Accept or Ask AI for all', () => {
  const assign = vi.fn();
  vi.stubGlobal('location', {assign, href: 'http://preview.test/workspace/'});
  mount();
  const content = document.getElementById('page-content');

  for (const form of [formOf('field', 'accept'), document.querySelector('[data-proposal-ask-all]').form]) {
    expect(swap(form, content, '<html><body>Log in</body></html>').detail.shouldSwap).toBe(false);
  }
  expect(assign).toHaveBeenCalledTimes(2);
});

it('a card and a workspace page swap as htmx answered them', () => {
  const assign = vi.fn();
  vi.stubGlobal('location', {assign, href: 'http://preview.test/workspace/'});
  mount(card('field', 'pending'));

  const polled = swap(cardOf('field'), cardOf('field'), card('field', 'completed'));
  const accepted = swap(formOf('field', 'cancel'), document.getElementById('page-content'), workspace([]));

  expect([polled.detail.shouldSwap, accepted.detail.shouldSwap]).toEqual([true, true]);
  expect(assign).not.toHaveBeenCalled();
});

it('a poll refused as missing, forbidden or logged out stops; a server error keeps polling', () => {
  for (const status of [401, 403, 404, 409]) {
    mount(card('field', 'pending'));
    refuse(cardOf('field'), status, {ok: false, error: `Refused ${status}.`});
    expect(cardOf('field').hasAttribute('data-proposal-halted')).toBe(true);
    expect(emit(cardOf('field'), 'htmx:beforeRequest').defaultPrevented).toBe(true);
  }
  mount(card('field', 'pending'));
  refuse(cardOf('field'), 500, '<html>Server Error</html>');
  expect(cardOf('field').hasAttribute('data-proposal-halted')).toBe(false);
  expect(errorOf('field').textContent).toBe('The proposal request was refused.');
  expect(emit(cardOf('field'), 'htmx:beforeRequest').defaultPrevented).toBe(false);
});

it('a hidden tab skips its polls and asks again once it shows', () => {
  mount(card('field', 'pending'));
  try {
    hide(true);
    expect(emit(cardOf('field'), 'htmx:beforeRequest').defaultPrevented).toBe(true);
    // An action is the operator's own request, so a hidden tab still sends it.
    expect(emit(formOf('field', 'cancel'), 'htmx:beforeRequest').defaultPrevented).toBe(false);
    hide(false);
    expect(emit(cardOf('field'), 'htmx:beforeRequest').defaultPrevented).toBe(false);
  } finally {
    delete document.hidden;
  }
});

it('an action refused because the session ended reads nothing more and stops the card', async () => {
  mount(card('field', 'pending'));

  refuse(formOf('field', 'cancel'), 401, {ok: false, error: 'Your session has ended. Reload the page to log in again.'});
  await settled();

  expect(reads).toEqual([]);
  expect(cardOf('field').hasAttribute('data-proposal-halted')).toBe(true);
  expect(errorOf('field').textContent).toBe('Your session has ended. Reload the page to log in again.');
});

it('a workspace form whose session ended reloads the page, which then shows the login page', () => {
  const assign = vi.fn();
  vi.stubGlobal('location', {assign, href: 'http://preview.test/workspace/'});
  mount();

  refuse(document.querySelector('[data-proposal-ask-all]').form, 401, {ok: false, error: 'Your session has ended.'});

  expect(assign).toHaveBeenCalledWith('http://preview.test/workspace/');
});

it('an older count that arrives after a newer one does not replace it', () => {
  mount();
  document.getElementById('ndiActiveProposals').dataset.countedAt = '100';

  const newer = swapCount(0, 300);
  document.getElementById('ndiActiveProposals').dataset.countedAt = '300';
  const older = swapCount(1, 200);
  const same = swapCount(0, 300);

  expect([newer.detail.shouldSwap, older.detail.shouldSwap, same.detail.shouldSwap]).toEqual([true, false, true]);
});
