/* gaceta live condiciones — vanilla, no deps.
 * The edition is printed once; a box whose provider offers live() is marked
 * data-live="1" by the renderer and refreshed in place: every 60 s while the
 * tab is visible we fetch /api/conditions (rendered server-side, in the
 * reader's language, escaped) and swap each box — and the masthead ear —
 * through a <template> parse. A failed or non-200 fetch leaves the snapshot
 * alone; boxes the server doesn't return keep their snapshot; and a live box
 * whose data ages past 20 min without a successful refresh admits it
 * ('último dato HH:MM', muted). Nothing runs from file:// (the static export
 * stays a paper), and there is no animation at all: reduced-motion users
 * get the same calm page. */
(function () {
  "use strict";
  if (location.protocol === "file:") return;
  var aside = document.querySelector("aside.conditions[data-reader]");
  if (!aside || !document.querySelector('.cond-box[data-live="1"]')) return;

  var L10N = window.GACETA_L10N || {};
  function tr(key, vars) {
    var text = L10N[key] || key;
    return text.replace(/\{(\w+)\}/g, function (m, k) {
      return vars && vars[k] !== undefined ? String(vars[k]) : m;
    });
  }

  var reader = aside.getAttribute("data-reader") || "";
  var maxPrivacy = aside.getAttribute("data-max-privacy") || "public";
  var TICK = 60 * 1000;        /* between refreshes */
  var STALE = 20 * 60 * 1000;  /* admit age past this */

  /* local wall-clock HH:MM (observed_at may be UTC, e.g. "...T18:21:32+00:00") */
  function hhmm(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function node(html) {
    var t = document.createElement("template");
    t.innerHTML = html; /* server-rendered by gaceta's own escaping renderer */
    return t.content.firstElementChild;
  }

  function apply(data) {
    ((data && data.boxes) || []).forEach(function (b) {
      var el = b.id ? document.getElementById("cond-" + b.id) : null;
      if (el && b.html) {
        var fresh = node(b.html);
        if (fresh) el.replaceWith(fresh);
      }
      var ear = document.getElementById("cond-ear");
      if (ear && b.ear_html && ear.getAttribute("data-cond") === "cond-" + b.id) {
        var freshEar = node(b.ear_html);
        if (freshEar) ear.replaceWith(freshEar);
      }
    });
  }

  /* No successful refresh: if the snapshot itself is old, say so, keeping any
   * source note that came after the ' · '. Idempotent, so it also holds on a
   * fragment the server already marked stale. */
  function markStale() {
    var now = Date.now();
    document.querySelectorAll('.cond-box[data-live="1"]').forEach(function (el) {
      var iso = el.getAttribute("data-observed");
      var at = iso ? Date.parse(iso) : NaN;
      if (isNaN(at) || now - at <= STALE) return;
      var stamp = el.querySelector(".cond-stamp");
      if (!stamp) return;
      stamp.classList.add("stale");
      var parts = stamp.textContent.split(" · ");
      parts[0] = tr("conditions.stale", { time: hhmm(iso) });
      stamp.textContent = parts.join(" · ");
    });
  }

  function tick() {
    if (document.visibilityState !== "visible") return;
    fetch("/api/conditions?reader=" + encodeURIComponent(reader) +
          "&max_privacy=" + encodeURIComponent(maxPrivacy))
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(apply)
      .catch(function () { /* keep the snapshot */ });
    markStale();
  }

  var timer = null;
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      tick();                       /* back in front: refresh at once */
      if (!timer) timer = setInterval(tick, TICK);
    } else if (timer) {             /* hidden: no ticks, no fetches */
      clearInterval(timer);
      timer = null;
    }
  });
  tick();
  timer = setInterval(tick, TICK);
})();
