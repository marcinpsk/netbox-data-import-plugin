/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* htmx swaps each termination card in place. A refused proposal command or read answers with the
 * workspace JSON envelope and not with a card, so this script shows that refusal in its card. It also
 * keeps a page that is not a card, such as the login page, out of the cards. */
(function () {
  if (window.ndiTraceProposals) return;
  window.ndiTraceProposals = true;

  // A read refused with one of these is refused again on the next interval, so the card stops polling.
  var HALTING = [401, 403, 404, 409];

  function scopeOf(event) {
    var source = event.detail.elt;
    return source && source.closest ? source.closest('[data-proposal-field], [data-proposal-batch]') : null;
  }

  function cardOf(event) {
    var scope = scopeOf(event);
    return scope && scope.matches('[data-proposal-field]') ? scope : null;
  }

  function show(card, message) {
    var slot = card.querySelector('[data-proposal-error]');
    slot.textContent = message;
    slot.hidden = !message;
  }

  function halt(card, message) {
    card.setAttribute('data-proposal-halted', '');
    show(card, message);
  }

  function refusal(xhr) {
    try {
      var payload = JSON.parse(xhr.responseText);
      if (payload && typeof payload.error === 'string') return payload;
    } catch (_) {
      // Not the JSON envelope, for example a NetBox error page; the generic sentence follows.
    }
    return {error: 'The proposal request was refused.'};
  }

  document.addEventListener('htmx:beforeRequest', function (event) {
    var card = cardOf(event);
    if (!card) return;
    if (event.detail.elt !== card) {
      show(card, '');
    } else if (card.hasAttribute('data-proposal-halted') || document.hidden) {
      // A halted card stays quiet, and a hidden tab asks again on the first interval after it shows.
      event.preventDefault();
    }
  });

  document.addEventListener('htmx:beforeSwap', function (event) {
    var scope = scopeOf(event);
    if (!scope || !event.detail.shouldSwap) return;
    var expected = event.detail.target.matches('[data-proposal-field]') ? 'data-proposal-field=' : 'id="page-content"';
    if (event.detail.xhr.responseText.indexOf(expected) !== -1) return;
    // Another page came back, for example the login page after the session ended, so the browser opens it.
    event.detail.shouldSwap = false;
    if (scope.matches('[data-proposal-field]')) halt(scope, 'NetBox answered with another page. Opening it.');
    window.location.assign(event.detail.xhr.responseURL || window.location.href);
  });

  document.addEventListener('htmx:responseError', function (event) {
    var card = cardOf(event);
    if (!card) return;
    var status = event.detail.xhr.status;
    var answer = refusal(event.detail.xhr);
    if (event.detail.elt === card) {
      if (HALTING.indexOf(status) === -1) show(card, answer.error);
      else halt(card, answer.error);
      return;
    }
    if (answer.code === 'preview_stale' || status === 401) {
      halt(card, answer.error);
      return;
    }
    // The action can lose a race with another operator, so the card first shows what the field holds now.
    // The refused request still holds its card until this event returns, and htmx would queue the read.
    setTimeout(function () {
      window.htmx.ajax('GET', card.dataset.proposalRead, {source: card, target: card, swap: 'outerHTML'})
        .then(function () { show(document.getElementById(card.id) || card, answer.error); });
    }, 0);
  });

  document.addEventListener('htmx:sendError', function (event) {
    var card = cardOf(event);
    if (card) show(card, 'The proposal request did not reach NetBox. Try again.');
  });
}());
