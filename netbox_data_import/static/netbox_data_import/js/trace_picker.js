/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* The three trace pickers share one dialog behavior: open for one question, search, count, page,
 * select one server-offered candidate, and submit it. Each picker states only its endpoint question,
 * how one candidate row reads, and the fields it submits. */
(function () {
  // The script ships inside the swapped content, so an htmx boost evaluates it again on every
  // navigation. Document listeners outlive the swap, so a second evaluation would double them.
  if (window.ndiTracePickers) return;

  // A swap replaces every node a picker reads, so each one is read at the time it is used.
  function node(id) {
    return document.getElementById(id);
  }

  function show(target, visible) {
    if (target) target.hidden = !visible;
  }

  function modalClass() {
    // NetBox/Tabler exposes Bootstrap as global `Modal`, not `bootstrap.Modal`.
    return (typeof bootstrap !== 'undefined' && bootstrap.Modal) || window.Modal;
  }

  // htmx replaces the content the dialog lives in, so an open dialog would strand its backdrop.
  document.addEventListener('htmx:beforeRequest', function (event) {
    var modal = event.target.closest && event.target.closest('.modal');
    var ModalClass = modalClass();
    var instance = modal && ModalClass && ModalClass.getInstance && ModalClass.getInstance(modal);
    if (instance) instance.hide();
  });

  function countText(payload, noun) {
    var shown = payload.shown || 0;
    var offset = payload.offset || 0;
    var range = offset ? (offset + 1) + '–' + (offset + shown) : String(shown);
    return range + ' of ' + (payload.total || 0) + ' ' + noun;
  }

  function createPicker(config) {
    var prefix = config.prefix;
    var pending = 0;
    var timer = null;
    var page = {offset: 0, limit: 0};

    function part(name) {
      return node(prefix + name);
    }

    function rows() {
      var list = part('Candidates');
      return list ? Array.prototype.slice.call(list.children) : [];
    }

    function clearSelection() {
      rows().forEach(function (row) {
        row.classList.remove('active');
        row.setAttribute('aria-pressed', 'false');
      });
      Object.keys(config.submits).forEach(function (field) {
        part(field).value = '';
      });
      part('Submit').disabled = true;
    }

    function showPages(payload) {
      var shown = payload.shown || 0;
      page = {offset: payload.offset || 0, limit: payload.limit || shown};
      part('Previous').disabled = page.offset === 0;
      part('Next').disabled = page.offset + shown >= (payload.total || 0);
      show(part('Pages'), !(part('Previous').disabled && part('Next').disabled));
    }

    function hideAnswer() {
      clearSelection();
      part('Candidates').replaceChildren();
      show(part('Count'), false);
      show(part('Pages'), false);
    }

    function select(item, candidate, offer) {
      rows().forEach(function (row) {
        row.classList.remove('active');
        row.setAttribute('aria-pressed', 'false');
      });
      item.classList.add('active');
      item.setAttribute('aria-pressed', 'true');
      Object.keys(config.submits).forEach(function (field) {
        // The offer belongs to the query that produced it, not to whatever the box says on click.
        part(field).value = config.submits[field](candidate, offer);
      });
      part('Submit').disabled = !config.ready(candidate);
    }

    function render(payload, offer) {
      var list = part('Candidates');
      clearSelection();
      list.replaceChildren();
      (payload.candidates || []).forEach(function (candidate) {
        var item = document.createElement('button');
        item.type = 'button';
        item.className = 'list-group-item list-group-item-action ' + (config.rowClass || '');
        item.setAttribute('aria-pressed', 'false');
        item.dataset.candidateId = candidate.id;
        config.render(item, candidate);
        item.addEventListener('click', function () { select(item, candidate, offer); });
        list.appendChild(item);
      });
      part('Count').textContent = countText(payload, config.noun);
      show(part('Count'), true);
      showPages(payload);
    }

    function reportFailure(message) {
      hideAnswer();
      part('Error').textContent = message;
      show(part('Error'), true);
    }

    function load(offset) {
      var form = part('Form');
      var search = part('Search');
      // A boost can land on a page with no picker while a debounce is still pending.
      if (!form || !search) return;
      // An explicit read supersedes a scheduled one, which would otherwise land after it.
      window.clearTimeout(timer);
      var request = ++pending;
      var offer = {search: search.value, offset: offset};
      function current() {
        // A slower earlier search must not overwrite a later one, and an answer to the page a
        // boost replaced must not be shown on the page that replaced it.
        return request === pending && part('Form') === form;
      }
      var url = form.dataset.candidatesUrl + '?' + config.keyParam + '=' + encodeURIComponent(part('Key').value)
        + '&search=' + encodeURIComponent(offer.search)
        + '&offset=' + offer.offset
        + '&preview_revision=' + encodeURIComponent(form.elements.namedItem('preview_revision').value);
      fetch(url, {headers: {Accept: 'application/json'}, credentials: 'same-origin'})
        .then(function (response) {
          return response.json().then(function (payload) {
            return {ok: response.ok, payload: payload};
          });
        })
        .then(function (result) {
          if (!current()) return;
          if (!result.ok || !result.payload.ok) {
            reportFailure(result.payload.error || 'The candidates could not be read.');
            return;
          }
          show(part('Error'), false);
          render(result.payload, offer);
        })
        .catch(function () {
          if (current()) reportFailure('The candidates could not be read.');
        });
    }

    var search = {
      load: load,
      // Scheduling a search retires the one in flight, so an answer to a superseded query never renders.
      reschedule: function (delay) {
        pending += 1;
        window.clearTimeout(timer);
        timer = window.setTimeout(function () { load(0); }, delay);
      }
    };

    function open(trigger) {
      var modal = part('Picker');
      var ModalClass = modalClass();
      if (!modal || !part('Form') || !ModalClass) return;
      part('Key').value = config.keyOf(trigger) || '';
      part('Label').textContent = config.labelOf(trigger) || '';
      part('Search').value = '';
      show(part('Error'), false);
      hideAnswer();
      search.load(0);
      modal.addEventListener('hidden.bs.modal', function () { trigger.focus(); }, {once: true});
      ModalClass.getOrCreateInstance(modal).show(trigger);
    }

    return {
      prefix: prefix,
      trigger: config.trigger,
      open: open,
      typed: function () {
        // The debounce leaves a window in which the old selection, or the old pages, could still be used.
        clearSelection();
        part('Previous').disabled = true;
        part('Next').disabled = true;
        search.reschedule(200);
      },
      turn: function (direction) {
        clearSelection();
        search.load(direction > 0 ? page.offset + page.limit : Math.max(0, page.offset - page.limit));
      }
    };
  }

  function factText(prefix, fact) {
    var compared = fact.mapped
      ? 'source ' + fact.source + ', mapped to ' + fact.mapped + ', NetBox ' + fact.netbox
      : 'source ' + fact.source + ', NetBox ' + fact.netbox;
    return prefix + ' ' + fact.fact + ': ' + compared;
  }

  function appendLine(item, className, text) {
    var line = document.createElement('div');
    if (className) line.className = className;
    line.textContent = text;
    item.appendChild(line);
  }

  var offered = {
    OfferedSearch: function (candidate, offer) { return offer.search; },
    OfferedOffset: function (candidate, offer) { return String(offer.offset); }
  };

  var pickers = [
    createPicker({
      prefix: 'traceDevice',
      trigger: '[data-trace-device-picker]',
      keyOf: function (trigger) { return trigger.dataset.traceDevicePicker; },
      labelOf: function (trigger) { return trigger.dataset.traceDeviceLabel; },
      keyParam: 'device_key',
      noun: 'eligible',
      render: function (item, candidate) {
        appendLine(item, '', candidate.display || candidate.name);
        (candidate.matched_facts || []).forEach(function (fact) {
          appendLine(item, 'text-secondary small', factText('Matches', fact));
        });
        (candidate.conflicting_facts || []).forEach(function (fact) {
          appendLine(item, 'text-secondary small', factText('Differs', fact));
        });
        var hint = candidate.import_location;
        if (hint) {
          appendLine(item, 'text-secondary small', 'In import Location ' + hint.location + ' (NetBox ' + hint.netbox + ')');
        }
      },
      submits: {
        Id: function (candidate) { return candidate.id; },
        OfferedSearch: offered.OfferedSearch,
        OfferedOffset: offered.OfferedOffset
      },
      ready: function () { return true; }
    }),
    createPicker({
      prefix: 'traceLocation',
      trigger: '[data-trace-location-picker]',
      keyOf: function (trigger) { return trigger.dataset.traceLocationPicker; },
      labelOf: function (trigger) { return trigger.dataset.traceLocationLabel; },
      keyParam: 'location_key',
      noun: 'visible Locations',
      // One line per Location keeps a full page of candidates and the Save button in view together.
      rowClass: 'd-flex justify-content-between align-items-baseline gap-3',
      render: function (item, candidate) {
        var title = document.createElement('span');
        title.textContent = candidate.name;
        item.appendChild(title);
        if (candidate.parent) {
          var parent = document.createElement('span');
          parent.className = 'text-secondary small text-end';
          parent.textContent = 'In ' + candidate.parent;
          item.appendChild(parent);
        }
      },
      submits: {
        Id: function (candidate) { return candidate.id; }
      },
      ready: function () { return true; }
    }),
    createPicker({
      prefix: 'traceTermination',
      trigger: '[data-trace-picker]',
      keyOf: function (trigger) { return trigger.dataset.tracePicker; },
      labelOf: function (trigger) { return trigger.dataset.traceLabel; },
      keyParam: 'field_key',
      noun: 'eligible',
      render: function (item, candidate) {
        item.textContent = candidate.display || candidate.name;
        // One claim admits several models, so each candidate names its own.
        if (candidate.model) {
          var model = document.createElement('span');
          model.className = 'badge text-bg-secondary ms-2';
          model.textContent = candidate.model;
          item.append(' ', model);
        }
      },
      submits: {
        ObjectId: function (candidate) { return candidate.id; },
        // Two models can share one id, so the model travels with the id and is never derived.
        ObjectType: function (candidate) { return candidate.object_type || ''; },
        OfferedSearch: offered.OfferedSearch,
        OfferedOffset: offered.OfferedOffset
      },
      ready: function (candidate) { return Boolean(candidate.object_type); }
    })
  ];
  window.ndiTracePickers = pickers;

  function owner(target, suffix) {
    return pickers.find(function (picker) { return target.id === picker.prefix + suffix; });
  }

  document.addEventListener('click', function (event) {
    for (var index = 0; index < pickers.length; index += 1) {
      var trigger = event.target.closest(pickers[index].trigger);
      if (trigger) {
        pickers[index].open(trigger);
        return;
      }
    }
    var previous = owner(event.target, 'Previous');
    if (previous) previous.turn(-1);
    var next = owner(event.target, 'Next');
    if (next) next.turn(1);
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && owner(event.target, 'Search')) event.preventDefault();
  });

  document.addEventListener('input', function (event) {
    var picker = owner(event.target, 'Search');
    if (picker) picker.typed();
  });
})();
