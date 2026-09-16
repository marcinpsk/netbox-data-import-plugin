/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* One debounced candidate search, shared by the trace pickers.
 * Scheduling a search retires the one in flight, so an answer to a superseded query can never
 * render. Both pickers held their own copy of this and both let that answer land. */
(function () {
  if (window.ndiPickerSearch) return;

  window.ndiPickerSearch = function (options) {
    var pending = 0;
    var timer = null;

    function run() {
      var form = options.form();
      var search = options.search();
      // A boost can land on a page with no picker while a debounce is still pending.
      if (!form || !search) return;
      var request = ++pending;
      var asked = search.value;
      function current() {
        // A slower earlier search must not overwrite a later one, and an answer to the page a
        // boost replaced must not be shown on the page that replaced it.
        return request === pending && options.form() === form;
      }
      fetch(options.url(form, asked), {headers: {Accept: 'application/json'}, credentials: 'same-origin'})
        .then(function (response) {
          return response.json().then(function (payload) {
            return {ok: response.ok, payload: payload};
          });
        })
        .then(function (result) {
          if (!current()) return;
          if (!result.ok || !result.payload.ok) {
            options.onError(result.payload.error || 'The candidates could not be read.');
            return;
          }
          options.onResult(result.payload, asked);
        })
        .catch(function () {
          if (current()) options.onError('The candidates could not be read.');
        });
    }

    return {
      load: run,
      reschedule: function (delay) {
        pending += 1;
        window.clearTimeout(timer);
        timer = window.setTimeout(run, delay);
      }
    };
  };
})();
