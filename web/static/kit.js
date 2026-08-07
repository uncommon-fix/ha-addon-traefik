/* ---------------------------------------------------------------------------
 * VENDORED FILE -- DO NOT EDIT HERE.
 * Source of truth: shared/web/kit.js, in the private workspace repo.
 * Copied by tools/sync-shared.ps1. An edit made to THIS copy is drift:
 * sync-shared.ps1 -Check reports it, and the next sync overwrites it.
 * --------------------------------------------------------------------------- */

/* kit.js -- the two things every kit view needs, and nothing else.
 *
 * Deliberately NOT a framework. The add-ons already disagree about that
 * (traefik and davinci use Alpine, unifi uses plain DOM) and the kit has no
 * business settling it. This file is dependency-free, loads with a plain
 * <script src>, adds one global, and works identically under all three.
 *
 *   kit.base()        the validated ingress prefix, or "" outside ingress
 *   kit.url(path)     that prefix joined to an app-relative path
 *   kit.pollState()   poll GET /api/state until told to stop
 *   kit.reloadOnLeave(state)   the "starting" page's whole job
 *
 * The prefix comes from a <meta name="ingress-path"> the server renders from
 * ingress.ingress_path(), which is whitelisted and HTML-escaped there. The
 * browser un-escapes it when reading .content, so this file must never
 * re-escape or re-validate -- one authority, server-side.
 */
(function (global) {
  'use strict';

  var _base = null;

  function base() {
    if (_base === null) {
      var meta = document.querySelector('meta[name="ingress-path"]');
      _base = (meta && meta.content) || '';
      // A trailing slash here plus a leading slash in url() is the
      // double-slash bug every add-on has written at least once.
      _base = _base.replace(/\/+$/, '');
    }
    return _base;
  }

  function url(path) {
    var p = path || '/';
    if (p.charAt(0) !== '/') { p = '/' + p; }
    return base() + p;
  }

  /* Poll a state endpoint. Returns a handle with .stop().
   *
   * options: { path, interval, onState(state, body), onError(err) }
   *
   * The next tick is scheduled only after the previous one settles, so a
   * backend that takes eight seconds to answer produces one request in
   * flight, not three -- which matters because the state we are usually
   * waiting on is "the service is still starting and the box is busy".
   */
  function pollState(options) {
    var o = options || {};
    var path = o.path || '/api/state';
    var interval = o.interval || 3000;
    var onState = o.onState || function () {};
    var onError = o.onError || function () {};
    var stopped = false;
    var timer = null;

    function schedule() {
      if (!stopped) { timer = setTimeout(tick, interval); }
    }

    function tick() {
      fetch(url(path), { headers: { Accept: 'application/json' } })
        .then(function (r) {
          return r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status));
        })
        .then(function (body) {
          if (!stopped) { onState(body && body.state, body); }
        })
        .catch(function (err) {
          // An unreachable backend IS the state we are waiting out; a
          // starting page that shows an error box on every poll is noise.
          if (!stopped) { onError(err); }
        })
        .then(schedule, schedule);
    }

    tick();
    return {
      stop: function () { stopped = true; clearTimeout(timer); }
    };
  }

  /* Reload once the server stops reporting `state`.
   *
   * The starting page cannot know which view comes next -- that is the
   * server's decision, made in Views.template_for(). So it does not choose:
   * it reloads and lets GET / answer again.
   */
  function reloadOnLeave(state, options) {
    var o = options || {};
    return pollState({
      path: o.path,
      interval: o.interval,
      onState: function (current) {
        if (current && current !== state) { global.location.reload(); }
      }
    });
  }

  global.kit = {
    base: base,
    url: url,
    pollState: pollState,
    reloadOnLeave: reloadOnLeave
  };
})(window);
