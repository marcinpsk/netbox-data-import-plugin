/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The proposal cards under the htmx release NetBox ships, so each swap, poll and busy state is real. */
import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {ACTION_URLS, ASK_ALL_URL, answer, card, workspace} from '../js/trace_proposal_fixture.js';
import {postedFields, scriptSource} from './preview_page.js';

const ORIGIN = 'http://preview.test';
const WORKSPACE_URL = `${ORIGIN}/plugins/data-import/trace-workspace/`;
// The proposal routes sit under the workspace path, so each route matches one exact path.
const atPath = path => url => url.pathname === path;
const workspacePage = atPath('/plugins/data-import/trace-workspace/');
const cardRead = atPath('/plugins/data-import/trace-workspace/proposals/');
const htmxSource = readFileSync('node_modules/htmx.org/dist/htmx.min.js', 'utf8');
// NetBox's own stylesheet builds on Bootstrap, which gives the spinner its size.
const bootstrapCss = readFileSync('node_modules/bootstrap/dist/css/bootstrap.min.css', 'utf8');
const scripts = `<script>${scriptSource('preview_claim.js')}</script><script>${scriptSource('trace_proposals.js')}</script>`;
const cardOf = (page, field) => page.locator(`[data-proposal-field="${field}"]`);
const actionOf = (page, field, key) => cardOf(page, field).locator(`[data-proposal-action="${key}"]`);

function document(content) {
  return `<!doctype html><html><head><meta charset="utf-8">
    <style>${bootstrapCss}</style>
    <style>.ndi-busy { display: none; } .htmx-request .ndi-busy { display: inline-block; }</style>
    <script>${htmxSource}</script></head><body>${content}${scripts}</body></html>`;
}

/* Serves the workspace at its URL and returns how many full page loads the browser made. */
async function open(page, cards) {
  let loads = 0;
  await page.route(workspacePage, route => {
    loads += 1;
    return route.fulfill({contentType: 'text/html; charset=utf-8', body: document(workspace(cards))});
  });
  await page.goto(WORKSPACE_URL);
  // A reload would build a new window, so this survives only while the page swaps in place.
  await page.evaluate(() => { window.ndiSamePage = true; });
  return () => loads;
}

const samePage = page => page.evaluate(() => window.ndiSamePage === true);
const html = body => ({contentType: 'text/html; charset=utf-8', body});

test('Ask AI on two fields posts one claim twice, shows both busy, and swaps each card without a reload', async ({page}) => {
  const held = [];
  await page.route(`${ORIGIN}${ACTION_URLS.request}`, async route => {
    held.push({route, posted: await postedFields(route.request())});
  });
  const loads = await open(page, [card('first'), card('second')]);

  await actionOf(page, 'first', 'request').click();
  await actionOf(page, 'second', 'request').click();

  await expect.poll(() => held.length).toBe(2);
  for (const field of ['first', 'second']) {
    await expect(actionOf(page, field, 'request')).toBeDisabled();
    await expect(actionOf(page, field, 'request').locator('.ndi-busy')).toBeVisible();
    // The other action of a busy card waits too, so one card never sends two commands at once.
    await expect(actionOf(page, field, 'cancel')).toBeDisabled();
  }
  expect(held.map(({posted}) => [posted.field_key, posted.preview_revision, posted.csrfmiddlewaretoken])).toEqual([
    ['first', '4', 'fixture-token'], ['second', '4', 'fixture-token'],
  ]);

  await held[1].route.fulfill(html(answer('second', 'pending', 1, 200)));
  await expect(page.locator('#ndiActiveProposals')).toHaveText('1');
  await held[0].route.fulfill(html(answer('first', 'pending', 2, 300)));
  await expect(page.locator('#ndiActiveProposals')).toHaveText('2');

  for (const field of ['first', 'second']) {
    await expect(cardOf(page, field).locator('[data-proposal-progress]')).toHaveText('Waiting for the backend...');
    await expect(actionOf(page, field, 'cancel')).toBeEnabled();
    await expect(actionOf(page, field, 'request').locator('.ndi-busy')).toBeHidden();
  }
  expect(await samePage(page)).toBe(true);
  expect(loads()).toBe(1);
});

test('a pending card polls its own read every three seconds and stops once it settles', async ({page}) => {
  await page.clock.install({time: new Date('2026-09-11T08:00:00Z')});
  const asked = [];
  await page.route(cardRead, route => {
    asked.push(new URL(route.request().url()).searchParams);
    return route.fulfill(html(card('first', 'completed')));
  });
  await open(page, [card('first', 'pending'), card('second')]);

  await page.clock.runFor(2900);
  expect(asked).toHaveLength(0);
  await page.clock.runFor(200);
  await expect(cardOf(page, 'first').locator('[data-proposal-badge]')).toHaveText('Proposal - not applied');
  expect([...asked[0]]).toEqual([
    ['field_key', 'first'], ['trace', 'trace-1'], ['preview_token', 'token-1'], ['preview_revision', '4'],
    ['preview_document', '11'], ['preview_profile', '3'],
  ]);
  await page.clock.runFor(12000);
  expect(asked).toHaveLength(1);
});

test('Accept swaps the replanned workspace, and every form then carries the advanced claim', async ({page}) => {
  const posted = [];
  await page.route(`${ORIGIN}${ACTION_URLS.accept}`, async route => {
    posted.push(await postedFields(route.request()));
    // The view redirects to the workspace; a routed XHR cannot follow a redirect, so this answers with that page.
    await route.fulfill(html(document(workspace([card('second', 'open', {revision: 5})], {revision: 5, note: 'Replanned.'}))));
  });
  const loads = await open(page, [card('first', 'completed'), card('second')]);

  await actionOf(page, 'first', 'accept').click();

  await expect(page.locator('#ndi-note')).toHaveText('Replanned.');
  await expect(cardOf(page, 'first')).toHaveCount(0);
  await expect(page.locator('#ndi-revision')).toHaveText('5');
  const revisions = await page.locator('input[name="preview_revision"]').evaluateAll(inputs => inputs.map(input => input.value));
  expect(revisions.length).toBeGreaterThan(3);
  expect(new Set(revisions)).toEqual(new Set(['5']));
  expect(posted).toEqual([expect.objectContaining({proposal_id: '7', preview_revision: '4', field_key: 'first'})]);
  expect(await samePage(page)).toBe(true);
  expect(loads()).toBe(1);
});

test('a refused action reads its card again and shows the refusal there, without a reload', async ({page}) => {
  await page.route(`${ORIGIN}${ACTION_URLS.request}`, route => route.fulfill({
    status: 409, json: {ok: false, error: 'This field already has an active Resolution Proposal.'},
  }));
  await page.route(cardRead, route =>
    route.fulfill(html(card('first', 'pending'))));
  const loads = await open(page, [card('first'), card('second')]);

  await actionOf(page, 'first', 'request').click();

  await expect(cardOf(page, 'first').locator('[data-proposal-progress]')).toBeVisible();
  await expect(cardOf(page, 'first').locator('[data-proposal-error]')).toHaveText(
    'This field already has an active Resolution Proposal.');
  await expect(cardOf(page, 'second').locator('[data-proposal-error]')).toBeHidden();
  expect(await samePage(page)).toBe(true);
  expect(loads()).toBe(1);
});

test('a poll the server refuses stops polling and says why', async ({page}) => {
  await page.clock.install({time: new Date('2026-09-11T08:00:00Z')});
  let reads = 0;
  await page.route(cardRead, route => {
    reads += 1;
    return route.fulfill({status: 409, json: {ok: false, error: 'A newer preview replaced this one.', code: 'preview_stale'}});
  });
  await open(page, [card('first', 'pending')]);

  await page.clock.runFor(3100);
  await expect(cardOf(page, 'first').locator('[data-proposal-error]')).toHaveText('A newer preview replaced this one.');
  await page.clock.runFor(12000);
  expect(reads).toBe(1);
});

test('Ask AI for all shows its busy state, then swaps in the workspace that names what it did', async ({page}) => {
  let release;
  const answered = new Promise(resolve => { release = resolve; });
  const posted = [];
  await page.route(`${ORIGIN}${ASK_ALL_URL}`, async route => {
    posted.push(await postedFields(route.request()));
    await answered;
    await route.fulfill(html(document(workspace([card('first', 'pending'), card('second', 'pending')], {
      note: 'Asked AI about 2 terminations.',
    }))));
  });
  const loads = await open(page, [card('first'), card('second')]);
  const askAll = page.locator('[data-proposal-ask-all]');

  await askAll.click();

  await expect(askAll).toBeDisabled();
  await expect(askAll.locator('.ndi-busy')).toBeVisible();
  release();
  await expect(page.locator('#ndi-note')).toHaveText('Asked AI about 2 terminations.');
  await expect(page.locator('[data-proposal-progress]')).toHaveCount(2);
  expect(posted).toEqual([expect.objectContaining({preview_revision: '4', trace: 'trace-1'})]);
  expect(await samePage(page)).toBe(true);
  expect(loads()).toBe(1);
});

test('a card answer that arrives late does not put back an older active proposal count', async ({page}) => {
  const held = [];
  await page.route(`${ORIGIN}${ACTION_URLS.cancel}`, route => { held.push(route); });
  await open(page, [card('first', 'pending'), card('second', 'pending')]);

  await actionOf(page, 'first', 'cancel').click();
  await actionOf(page, 'second', 'cancel').click();
  await expect.poll(() => held.length).toBe(2);
  // The first cancel counted before the second one committed, but its answer is delivered last.
  await held[1].fulfill(html(answer('second', 'open', 0, 300)));
  await expect(page.locator('#ndiActiveProposals')).toHaveText('0');
  await held[0].fulfill(html(answer('first', 'open', 1, 200)));

  await expect(actionOf(page, 'first', 'request')).toBeEnabled();
  await expect(page.locator('#ndiActiveProposals')).toHaveText('0');
});

test('a login page answered to Accept opens as a page instead of emptying the workspace', async ({page}) => {
  await page.route(`${ORIGIN}${ACTION_URLS.accept}`, route =>
    route.fulfill(html('<!doctype html><html><body><form id="login">Log in</form></body></html>')));
  await open(page, [card('first', 'completed')]);

  await actionOf(page, 'first', 'accept').click();

  await page.waitForURL(`${ORIGIN}${ACTION_URLS.accept}`);
  await expect(page.locator('#login')).toHaveText('Log in');
});
