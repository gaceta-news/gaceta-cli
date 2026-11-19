/* gaceta reactions + seen — vanilla, no deps, light/dark via currentColor.
 * Reads the reader from /r/<reader>/..., the person from localStorage
 * ('gaceta.by'), posts toggles to /api/react, restores pressed state from
 * /api/state. '¿por qué?' opens a panel with the story's score_reasons.
 *
 * Seen (per person, via /api/seen + /api/unseen): a story marked seen at its
 * current data-rev is hidden until the rev changes (new source or new
 * write-up); a changed story resurfaces with a 'Novedad' badge. Sections show
 * at most 12 unseen stories; 'más (N)' reveals more and 'Marcar sección como
 * vista' clears the section. A sticky counter shows how many new stories are
 * visible; 'mostrar vistas' reveals hidden seen stories dimmed.
 *
 * Cross-device: the server holds every mark; an open page re-reads /api/state
 * when it comes back into view (and every 90 s), so what the phone closed
 * disappears on the PC, and a rebuilt edition shows 'Edición nueva ↻'. */
(function () {
  "use strict";

  var m = /^\/r\/([a-z0-9][a-z0-9_-]*)\//.exec(location.pathname);
  if (!m) return;
  var reader = m[1];
  var dm = /(\d{4}-\d{2}-\d{2})\.html$/.exec(location.pathname);
  var date = dm ? dm[1] : null;
  var by = null;

  /* Interface strings: the server inlines window.GACETA_L10N for the page's
   * <html lang> (gaceta/locales/<lang>.json, "js" block). tr("key", {n: 2}). */
  var L10N = window.GACETA_L10N || {};
  function tr(key, vars) {
    var n = vars && typeof vars.n === "number" ? vars.n : null;
    var text = (n !== null && L10N[key + (n === 1 ? "_one" : "_other")]) || L10N[key] || key;
    return text.replace(/\{(\w+)\}/g, function (m, k) {
      return vars && vars[k] !== undefined ? String(vars[k]) : m;
    });
  }
  try { by = localStorage.getItem("gaceta.by"); } catch (e) { /* private mode */ }

  /* The bar shows three buttons (✓ Visto / Más así / Menos así) plus an ⓘ
   * link for the reasons panel. 'up'/'down' remain server kinds for old rows
   * but are no longer sent. */
  var DIM = { down: true, less: true, hide: true };
  var UNSEEN_SHOWN = 12;

  var seenState = {};   /* story id -> rev as marked on the server */
  var seenAt = {};      /* story id -> unix time of that mark ('Desde que la viste') */
  var showSeen = false; /* 'mostrar vistas' toggle */
  var topbar = null, counterEl = null, seenToggle = null, toastEl = null;

  function css() {
    var s = document.createElement("style");
    s.textContent =
      ".gaceta-rx{display:flex;margin:.9rem 0 0;max-width:34rem;border:1px solid var(--rule,#ccc);" +
      "border-radius:.75rem;overflow:hidden;clear:both;background:var(--card,transparent)}" +
      ".gaceta-rx button{appearance:none;flex:1 1 0;min-width:0;min-height:52px;margin:0;" +
      "padding:.4rem .15rem .35rem;border:0;border-left:1px solid var(--rule,#ccc);" +
      "background:transparent;color:var(--muted,inherit);font:inherit;font-family:system-ui,sans-serif;" +
      "font-size:.64rem;font-weight:600;letter-spacing:0;display:flex;flex-direction:column;" +
      "align-items:center;justify-content:center;gap:.2rem;cursor:pointer;white-space:nowrap;" +
      "transition:background .15s,color .15s;-webkit-tap-highlight-color:transparent}" +
      ".gaceta-rx button:first-child{border-left:0}" +
      ".gaceta-rx button .gtx{overflow:hidden;text-overflow:ellipsis;max-width:100%}" +
      ".gaceta-rx button:hover{color:var(--ink,inherit);background:color-mix(in srgb,var(--ink,#000) 4%,transparent)}" +
      ".gaceta-rx button:active{background:color-mix(in srgb,var(--ink,#000) 9%,transparent)}" +
      ".gaceta-rx button[data-kind=seen]{color:var(--ink,inherit)}" +
      ".gaceta-rx button[data-kind=agent]{color:var(--accent,inherit)}" +
      ".gaceta-rx button[aria-pressed=true]{background:var(--ink,#222);color:var(--bg,#fff)}" +
      ".gaceta-rx button[data-kind=save][aria-pressed=true]{background:transparent;color:var(--accent,inherit)}" +
      ".gaceta-rx button[data-kind=save][aria-pressed=true] svg{fill:currentColor}" +
      ".gaceta-rx button[aria-expanded=true]{background:color-mix(in srgb,var(--accent,#933) 12%,transparent)}" +
      ".gaceta-rx .gaceta-info{flex:0 0 2.3rem}" +
      ".gaceta-rx .gaceta-info .gtx{display:none}" +
      ".gaceta-compose{margin:.5rem 0 0;max-width:34rem}" +
      ".gaceta-compose textarea{box-sizing:border-box;width:100%;min-height:4.8rem;padding:.6rem .7rem;" +
      "border:1px solid var(--rule,#ccc);border-radius:.6rem;background:var(--card,transparent);" +
      "color:var(--ink,inherit);font:inherit;font-size:.95rem;resize:vertical}" +
      ".gaceta-compose textarea:focus{outline:2px solid var(--accent,#933);outline-offset:1px}" +
      ".gaceta-compose-row{display:flex;justify-content:flex-end;gap:.5rem;margin-top:.4rem}" +
      ".gaceta-compose-row button{appearance:none;min-height:44px;padding:0 1rem;border-radius:.6rem;" +
      "font:inherit;font-size:.9rem;cursor:pointer}" +
      ".gaceta-compose-cancel{background:none;border:0;color:var(--muted,inherit)}" +
      ".gaceta-compose-send{background:var(--accent,#933);color:var(--bg,#fff);border:0;font-weight:600}" +
      ".gaceta-compose-send:disabled{opacity:.5}" +
      ".gaceta-compose-done{margin:.5rem 0 0;color:var(--accent,inherit);font-size:.9rem}" +
      ".gaceta-why{margin:.55rem 0 0;padding:.1rem 0 .1rem .8rem;border-left:2px solid " +
      "var(--rule,#ccc);font-size:.88rem;color:var(--muted,inherit);max-width:30rem}" +
      ".gaceta-why strong{font-weight:600;color:var(--ink,inherit)}" +
      ".gaceta-why ul{margin:.2rem 0 0;padding-left:1rem}" +
      ".gaceta-done{display:flex;align-items:center;justify-content:space-between;gap:.6rem;" +
      "min-height:40px;margin-top:.6rem;padding:0 .2rem;" +
      "color:var(--muted,inherit);font-size:.85rem}" +
      ".gaceta-done button{appearance:none;background:none;border:0;min-height:44px;padding:0 .4rem;" +
      "color:var(--accent,inherit);font:inherit;text-decoration:underline;cursor:pointer}" +
      ".gaceta-bybar{position:fixed;left:50%;transform:translateX(-50%);" +
      "bottom:calc(1rem + env(safe-area-inset-bottom));z-index:9;display:flex;align-items:center;" +
      "gap:.5rem;flex-wrap:wrap;justify-content:center;max-width:min(92vw,30rem);padding:.6rem .9rem;" +
      "border-radius:1rem;border:1px solid var(--rule,#ccc);" +
      "background:color-mix(in srgb,var(--bg,#fff) 92%,transparent);backdrop-filter:blur(10px);" +
      "-webkit-backdrop-filter:blur(10px);color:var(--ink,#111);box-shadow:0 4px 18px rgba(0,0,0,.12);" +
      "font-size:.9rem}.gaceta-bybar button{appearance:none;background:transparent;color:inherit;" +
      "border:1px solid var(--rule,#ccc);border-radius:999px;min-height:44px;padding:.2rem .9rem;" +
      "font:inherit;cursor:pointer}" +
      ".gaceta-bybar input{min-height:44px;font:inherit;border-radius:999px;" +
      "border:1px solid var(--rule,#ccc);padding:.2rem .8rem;background:transparent;color:inherit}" +
      ".gaceta-topbar{position:fixed;bottom:calc(.7rem + env(safe-area-inset-bottom));right:.7rem;" +
      "z-index:9;display:flex;align-items:center;border-radius:999px;border:1px solid var(--rule,#ccc);" +
      "background:color-mix(in srgb,var(--bg,#fff) 88%,transparent);backdrop-filter:blur(10px);" +
      "-webkit-backdrop-filter:blur(10px);color:var(--ink,#111);box-shadow:0 2px 10px rgba(0,0,0,.08);" +
      "font-size:.78rem;line-height:1;transition:transform .2s,opacity .2s}" +
      ".gaceta-topbar[hidden]{display:none}" +
      ".gaceta-topbar.is-away{transform:translateY(150%);opacity:0}" +
      ".gaceta-topbar button{appearance:none;background:transparent;color:var(--muted,inherit);" +
      "border:0;border-left:1px solid var(--rule,#ccc);min-height:36px;padding:0 .7rem;font:inherit;" +
      "cursor:pointer;position:relative}" +
      ".gaceta-topbar button::after{content:'';position:absolute;inset:-4px 0}" +
      ".gaceta-topbar button[aria-pressed=true]{color:var(--accent,inherit)}" +
      ".gaceta-count{font-weight:700;padding:0 .7rem;white-space:nowrap}" +
      ".gaceta-topbar .gaceta-fresh{border-left:0;color:var(--accent,inherit);font-weight:700}" +
      ".gaceta-fresh+.gaceta-count{border-left:1px solid var(--rule,#ccc)}" +
      ".gaceta-sectools{display:flex;flex-wrap:wrap;gap:.2rem 1rem;margin:.9rem 0 0;" +
      "padding-top:.5rem;border-top:1px solid var(--rule,#ccc)}" +
      ".gaceta-sectools button{appearance:none;background:none;border:0;min-height:44px;padding:0;" +
      "color:var(--muted,inherit);font:inherit;font-size:.88rem;text-decoration:underline;" +
      "text-decoration-color:var(--rule,#ccc);text-underline-offset:.2em;cursor:pointer}" +
      ".gaceta-sectools button:hover{color:var(--ink,inherit)}" +
      ".gaceta-uptodate{margin:.8rem 0 0;color:var(--muted,inherit);font-size:.9rem;font-style:italic}" +
      ".gaceta-novelty{display:inline-block;margin-left:.4rem;padding:.12rem .4rem;border-radius:3px;" +
      "background:var(--accent,#933);color:var(--bg,#fff);font-family:system-ui,sans-serif;" +
      "font-size:.62rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;" +
      "vertical-align:.25em;white-space:nowrap}" +
      ".gaceta-toast{position:fixed;bottom:calc(4rem + env(safe-area-inset-bottom));left:50%;" +
      "transform:translateX(-50%);z-index:10;max-width:calc(100vw - 2rem);padding:.5rem .9rem;" +
      "border-radius:999px;background:var(--ink,#222);color:var(--bg,#fff);font-size:.85rem;" +
      "opacity:0;visibility:hidden;pointer-events:none;transition:opacity .2s,visibility .2s}" +
      ".gaceta-toast.is-on{opacity:1;visibility:visible}" +
      ".section article.gaceta-first{border-top:0}" +
      "section.section{overflow-anchor:none}.gaceta-closed+.section{margin-top:.6rem}" +
      ".gaceta-acc{display:flex;align-items:baseline;gap:.5rem;cursor:pointer;user-select:none;" +
      "-webkit-tap-highlight-color:transparent}" +
      ".gaceta-acc:focus-visible{outline:2px solid var(--accent,#933);outline-offset:2px}" +
      ".gaceta-acc-n{margin-left:auto;font-family:system-ui,sans-serif;font-size:.78rem;" +
      "font-weight:600;color:var(--muted,inherit)}" +
      ".gaceta-acc-chev{display:inline-block;font-size:.8em;color:var(--muted,inherit);" +
      "transition:transform .2s}" +
      ".gaceta-closed .gaceta-acc-chev{transform:rotate(-90deg)}" +
      ".gaceta-closed>:not(h2){display:none!important}" +
      ".section.gaceta-closed{margin:.2rem 0}" +
      ".gaceta-closed>h2{position:static;border-bottom-color:var(--rule,#ccc);color:var(--muted,inherit)}" +
      ".gaceta-src{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));" +
      "gap:.4rem;margin:.55rem 0 0;max-width:22rem;font:inherit}" +
      ".gaceta-src button{appearance:none;min-width:0;min-height:44px;padding:.3rem .25rem;" +
      "border:1px solid var(--rule,#ccc);border-radius:999px;background:transparent;" +
      "color:var(--muted,inherit);font:inherit;font-size:.85rem;white-space:nowrap;" +
      "cursor:pointer;transition:background .15s,color .15s,border-color .15s}" +
      ".gaceta-src button:hover{color:var(--ink,inherit);border-color:var(--muted,#999)}" +
      ".gaceta-src button[disabled]{opacity:.55;cursor:default}" +
      ".gaceta-src-done{display:block;margin-top:.55rem;color:var(--muted,inherit);" +
      "font-size:.85rem}";
    document.head.appendChild(s);
  }

  /* Writes in flight + a write epoch: a state snapshot fetched before (or while)
   * this page wrote something must not overwrite the optimistic local state. */
  var inflight = 0, writeEpoch = 0;

  function post(url, payload) {
    inflight++;
    writeEpoch++;
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (res) { inflight--; writeEpoch++; return res; });
  }

  function toast(msg) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "gaceta-toast";
      toastEl.setAttribute("role", "status");
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.classList.add("is-on");
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(function () { toastEl.classList.remove("is-on"); }, 2600);
  }

  /* ------------------------------------------------------------ implicit open
   * First link click inside an article (headline, Fuente, sources list) or a
   * YouTube play sends ONE 'open' beacon per story per page load. sendBeacon
   * would send text/plain; a Blob with type application/json keeps the
   * server's content-type check happy. */
  var openedSent = {};
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest || !by || !navigator.sendBeacon) return;
    var hit = t.closest("a[href]") || t.closest("button[data-yt]");
    var a = hit && t.closest("article[id]");
    if (!a || openedSent[a.id]) return;
    openedSent[a.id] = true;
    navigator.sendBeacon("/api/react", new Blob(
      [JSON.stringify({ reader: reader, date: date, story_id: a.id, kind: "open", by: by })],
      { type: "application/json" }));
  }, true);

  /* ------------------------------------------------------------ source verdicts */

  var SRC_DONE_LINE = {
    follow: function (n) { return tr("src.follow_done", { name: n }); },
    drop: function () { return tr("src.drop_done"); },
    later: function () { return tr("src.later_done"); }
  };

  function srcDoneLine(el, decision) {
    el.textContent = "";
    var line = document.createElement("span");
    line.className = "gaceta-src-done";
    var name = el.getAttribute("data-source-name") || "la fuente";
    line.textContent = SRC_DONE_LINE[decision]
      ? SRC_DONE_LINE[decision](name) : "✓";
    el.appendChild(line);
  }

  function sourceVerdicts(st) {
    var els = [].slice.call(document.querySelectorAll(".src-verdict[data-source-key]"));
    if (!els.length) return;
    var owner = st && st.owner;
    var isOwner = !!(by && owner &&
      by.toLowerCase() === String(owner).toLowerCase());
    els.forEach(function (el) {
      var key = el.getAttribute("data-source-key");
      var decided = st && st.source_decisions && st.source_decisions[key];
      if (decided) { srcDoneLine(el, decided); return; }
      if (!isOwner) return;  /* household readers see no buttons */
      var bar = document.createElement("div");
      bar.className = "gaceta-src";
      bar.setAttribute("role", "group");
      bar.setAttribute("aria-label", tr("src.aria"));
      [["follow", tr("src.follow")], ["drop", tr("src.drop")], ["later", tr("src.later")]].forEach(function (d) {
        var b = document.createElement("button");
        b.type = "button";
        b.textContent = d[1];
        b.addEventListener("click", function () {
          [].forEach.call(bar.querySelectorAll("button"), function (x) { x.disabled = true; });
          post("/api/source", { reader: reader, source_key: key, decision: d[0], by: by })
            .then(function (res) {
              if (!res || !res.ok) {
                [].forEach.call(bar.querySelectorAll("button"), function (x) { x.disabled = false; });
                toast(tr("err.save"));
                return;
              }
              srcDoneLine(el, d[0]);
            });
        });
        bar.appendChild(b);
      });
      el.appendChild(bar);
    });
  }

  /* ------------------------------------------------------------ seen logic */

  function revOf(a) { return a.getAttribute("data-rev") || ""; }
  function isSeenNow(a) { return !!seenState[a.id] && seenState[a.id] === revOf(a); }
  function isNew(a) { return !seenState[a.id] || seenState[a.id] !== revOf(a); }
  function articles() { return [].slice.call(document.querySelectorAll("article[id]")); }

  function hideArticle(a) {
    a.style.display = "none";
    a.removeAttribute("data-gaceta-extra");
  }

  var VERDICT = { more: tr("verdict.more"), less: tr("verdict.less"), seen: tr("verdict.seen_done") };

  function fadeOut(a, kind) {
    a.style.transition = "opacity .25s";
    a.style.opacity = "0";
    setTimeout(function () {
      hideArticle(a);
      refreshCount();
      if (kind) settle(undoRow(a, kind));
    }, 260);
  }

  /* Collapsing a long story yanks everything below it up past the viewport. Put the
   * verdict line just under the sticky section title, so the next headline is
   * right there, whatever the length of the story that was closed. */
  function settle(row) {
    if (!row) return;
    var sec = row.closest("section");
    var h = sec && sec.querySelector(":scope > h2");
    var pad = (h ? h.getBoundingClientRect().height : 0) + 8;
    var top = row.getBoundingClientRect().top;
    if (topbar) topbar.classList.add("is-away");  /* never cover the next verdict bar */
    if (Math.abs(top - pad) > 4) {
      var smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.scrollBy({ top: top - pad, behavior: smooth ? "smooth" : "auto" });
    }
  }

  /* After a verdict the story collapses into one quiet line; 'deshacer' stays for a few
   * seconds. The line itself stays (removing it would shift the page under the reader). */
  function undoRow(a, kind) {
    var row = document.createElement("div");
    row.className = "gaceta-done";
    row.setAttribute("role", "status");
    var t = document.createElement("span");
    t.textContent = "✓ " + (VERDICT[kind] || VERDICT.seen);
    var u = document.createElement("button");
    u.type = "button";
    u.textContent = tr("undo");
    row.appendChild(t);
    row.appendChild(u);
    a.parentNode.insertBefore(row, a.nextSibling);
    var gone = setTimeout(function () { u.style.visibility = "hidden"; }, 6000);
    u.addEventListener("click", function () {
      clearTimeout(gone);
      row.remove();
      if (kind === "more" || kind === "less") react(kind, a.id).then(function (res) {
        var bar = a.querySelector(".gaceta-rx");
        if (res && bar) syncBar(bar, res.state.kinds || []);
      });
      markUnseen(a).then(function () { applyBarSeen(a); });
      a.style.display = "";
      a.style.opacity = "";
      var hd = a.querySelector(".headline");
      if (hd) settle(hd);
    });
    return row;
  }

  function applyBarSeen(a) {
    var b = a.querySelector('.gaceta-rx button[data-kind="seen"]');
    if (b) b.setAttribute("aria-pressed", isSeenNow(a) ? "true" : "false");
  }

  function noveltyBadge(a) {
    if (a.querySelector(".gaceta-novelty")) return;
    var h = a.querySelector(".headline") || a;
    var b = document.createElement("span");
    b.className = "gaceta-novelty";
    b.textContent = tr("novelty");
    b.title = tr("novelty.title");
    b.setAttribute("aria-label", tr("novelty.title"));
    h.appendChild(b);
  }

  /* 'Desde que la viste': the renderer ships every rewrite with its time
   * (data-at) and each source with when it arrived (data-added); this person's
   * seen time comes from /api/state seen_at. Show the rewrites after that mark,
   * flag the sources that are new, and keep the text they read one tap away. */
  function fmtWhen(ts) {
    try {
      return new Date(ts * 1000).toLocaleString(L10N._locale || document.documentElement.lang || undefined, {
        day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
    } catch (e) { return ""; }
  }

  function showSince(a) {
    var box = a.querySelector(".since");
    var at = seenAt[a.id];
    [].forEach.call(a.querySelectorAll(".sources li .src-new"), function (n) { n.remove(); });
    if (!at) { if (box) box.hidden = true; return; }
    var newSrc = 0;
    [].forEach.call(a.querySelectorAll(".sources li[data-added]"), function (li) {
      if (+li.getAttribute("data-added") > at) {
        newSrc++;
        var tag = document.createElement("span");
        tag.className = "src-new";
        tag.textContent = tr("src.new");
        li.appendChild(tag);
      }
    });
    if (!box) return;
    var steps = 0;
    [].forEach.call(box.querySelectorAll(".since-steps li"), function (li) {
      var show = +li.getAttribute("data-at") > at;
      li.hidden = !show;
      if (show) steps++;
    });
    var before = null;
    [].forEach.call(box.querySelectorAll(".since-before p"), function (p) {
      p.hidden = true;
      if (+p.getAttribute("data-at") <= at) before = p;
    });
    if (before) before.hidden = false;
    var det = box.querySelector(".since-before");
    if (det) det.hidden = !before;
    var note = box.querySelector(".since-note");
    var bits = [];
    if (newSrc) bits.push(tr("since.new_src", { n: newSrc }));
    if (!steps && !newSrc) bits.push(tr("since.reworded"));
    if (note) note.textContent = bits.join(" · ");
    var label = box.querySelector(".since-label");
    var when = fmtWhen(at);
    if (label) label.textContent = tr("since.label") + (when ? " (" + when + ")" : "");
    box.hidden = !(steps || newSrc || before);
  }

  function hideSince(a) {
    var box = a.querySelector(".since");
    if (box) box.hidden = true;
    [].forEach.call(a.querySelectorAll(".sources li .src-new"), function (n) { n.remove(); });
  }

  function applySeen(a) {
    var s = seenState[a.id];
    if (!s) { hideSince(a); return; }
    if (s === revOf(a)) {
      a.style.display = showSeen ? "" : "none";
      a.style.opacity = showSeen ? ".45" : "";
    } else {
      noveltyBadge(a);
      showSince(a);
    }
  }

  function markSeen(items, kind) {  /* items: [{id, rev, article}] */
    var prev = {};
    items.forEach(function (it) { prev[it.id] = seenState[it.id]; });
    items.forEach(function (it) {
      seenState[it.id] = it.rev;
      if (it.article) fadeOut(it.article, items.length === 1 ? (kind || "seen") : null);
    });
    refreshCount();
    return post("/api/seen", {
      reader: reader, date: date, by: by,
      story_ids: items.map(function (it) { return it.id; }),
      revs: items.reduce(function (o, it) { o[it.id] = it.rev; return o; }, {})
    }).then(function (res) {
      if (res && res.ok) return;
      items.forEach(function (it) {
        if (prev[it.id] === undefined) delete seenState[it.id];
        else seenState[it.id] = prev[it.id];
        if (it.article) {
          it.article.style.display = "";
          it.article.style.opacity = "";
        }
      });
      arrangeSections();
      refreshCount();
      toast(tr("err.save"));
    });
  }

  function markUnseen(a) {
    var prev = seenState[a.id], prevOp = a.style.opacity;
    delete seenState[a.id];
    a.style.display = "";
    a.style.opacity = "";
    refreshCount();
    return post("/api/unseen", { reader: reader, story_id: a.id, by: by })
      .then(function (res) {
        if (res && res.ok) return;
        seenState[a.id] = prev;
        a.style.opacity = prevOp;
        applySeen(a);
        refreshCount();
        toast(tr("err.save"));
      });
  }

  /* ------------------------------------------------------------ layout */

  function sectionTitle(sec) {
    var h = sec.querySelector("h2");
    return h ? h.textContent.trim() : (sec.getAttribute("data-section") || "");
  }

  function sectools(sec) {
    var d = document.createElement("div");
    d.className = "gaceta-sectools";
    d.setAttribute("role", "group");
    d.setAttribute("aria-label", tr("sec.tools"));
    sec.appendChild(d);
    return d;
  }

  function uptodateLine(sec) {
    var p = document.createElement("p");
    p.className = "gaceta-uptodate";
    p.textContent = tr("sec.uptodate", { section: sectionTitle(sec) });
    sec.appendChild(p);
  }

  function arrangeSections() {
    [].slice.call(document.querySelectorAll("section[data-section]")).forEach(function (sec) {
      [].slice.call(sec.querySelectorAll(".gaceta-sectools,.gaceta-uptodate")).forEach(function (n) {
        n.remove();
      });
      var arts = [].slice.call(sec.querySelectorAll("article[id]"));
      arts.forEach(function (a) { if (a.getAttribute("data-gaceta-extra")) {
        a.style.display = "";
        a.removeAttribute("data-gaceta-extra");
      } });
      /* seen stories: hide (or show dimmed); changed ones get the badge */
      arts.forEach(applySeen);
      arts.forEach(function (a) { a.classList.remove("gaceta-first"); });
      var fresh = arts.filter(function (a) { return !isSeenNow(a); });
      var hiddenExtra = 0;
      fresh.forEach(function (a, i) {
        if (i >= UNSEEN_SHOWN) {
          a.style.display = "none";
          a.setAttribute("data-gaceta-extra", "1");
          hiddenExtra++;
        }
      });
      var firstVisible = arts.filter(function (a) { return a.style.display !== "none"; })[0];
      if (firstVisible) firstVisible.classList.add("gaceta-first");
      if (!fresh.length) { uptodateLine(sec); return; }
      var tools = sectools(sec);
      if (hiddenExtra) {
        var more = document.createElement("button");
        more.type = "button";
        var moreLabel = function (n) { more.textContent = tr("sec.more", { n: n }); };
        moreLabel(hiddenExtra);
        more.setAttribute("aria-label", tr("sec.more_aria", { section: sectionTitle(sec) }));
        var left = hiddenExtra;
        more.addEventListener("click", function () {
          var extras = [].slice.call(
            sec.querySelectorAll('article[data-gaceta-extra="1"]'));
          extras.slice(0, UNSEEN_SHOWN).forEach(function (a) {
            a.style.display = "";
            a.removeAttribute("data-gaceta-extra");
          });
          left = Math.max(0, left - UNSEEN_SHOWN);
          if (!left) more.remove();
          else moreLabel(left);
          refreshCount();
        });
        tools.appendChild(more);
      }
      var mark = document.createElement("button");
      mark.type = "button";
      mark.textContent = tr("sec.mark");
      mark.setAttribute("aria-label", tr("sec.mark_aria", { section: sectionTitle(sec) }));
      mark.addEventListener("click", function () {
        var visible = arts.filter(function (a) {
          return a.style.display !== "none" && !a.getAttribute("data-gaceta-extra") && isNew(a);
        });
        if (!visible.length) return;
        markSeen(visible.map(function (a) {
          return { id: a.id, rev: revOf(a), article: a };
        })).then(arrangeSections);
      });
      tools.appendChild(mark);
    });
    refreshCount();
  }

  /* Sections fold like an accordion. Folding is NOT a signal: nothing is sent, it is
   * remembered on this device for this reader until they open it again. */
  function closedKey() { return "gaceta.closed." + by; }
  function closedSet() {
    try { return JSON.parse(localStorage.getItem(closedKey()) || "{}"); } catch (e) { return {}; }
  }
  function saveClosed(m) { try { localStorage.setItem(closedKey(), JSON.stringify(m)); } catch (e) {} }

  function setupAccordions() {
    var closed = closedSet();
    [].slice.call(document.querySelectorAll("section.section[data-section]")).forEach(function (sec) {
      var h = sec.querySelector(":scope > h2");
      if (!h || h.classList.contains("gaceta-acc")) return;
      var id = sec.getAttribute("data-section");
      h.classList.add("gaceta-acc");
      h.setAttribute("role", "button");
      h.setAttribute("tabindex", "0");
      var chev = document.createElement("span");
      chev.className = "gaceta-acc-chev";
      chev.setAttribute("aria-hidden", "true");
      chev.textContent = "▾";
      h.insertBefore(chev, h.firstChild);
      var n = document.createElement("span");
      n.className = "gaceta-acc-n";
      h.appendChild(n);
      function apply(isClosed) {
        sec.classList.toggle("gaceta-closed", isClosed);
        h.setAttribute("aria-expanded", isClosed ? "false" : "true");
        h.title = isClosed ? tr("sec.open") : tr("sec.close");
      }
      apply(!!closed[id]);
      function toggle() {
        var m = closedSet();
        var isClosed = !sec.classList.contains("gaceta-closed");
        if (isClosed) m[id] = Date.now(); else delete m[id];
        saveClosed(m);
        /* the section's own top doesn't move when it folds (nothing above changes);
         * if the reader was inside it, land on its heading instead of wherever the
         * shrunken page would leave them */
        var secTop = sec.getBoundingClientRect().top + window.scrollY;
        var inside = sec.getBoundingClientRect().top < 0;
        apply(isClosed);
        refreshCount();
        if (inside) window.scrollTo(0, Math.max(0, secTop - 8));
      }
      h.addEventListener("click", toggle);
      h.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
      });
    });
  }

  function refreshSectionCounts() {
    [].slice.call(document.querySelectorAll("section.section[data-section]")).forEach(function (sec) {
      var el = sec.querySelector(":scope > h2 .gaceta-acc-n");
      if (!el) return;
      var n = [].slice.call(sec.querySelectorAll("article[id]")).filter(function (a) {
        return !isSeenNow(a);
      }).length;
      el.textContent = n ? tr("count.new", { n: n }) : tr("count.none");
    });
  }

  function refreshCount() {
    refreshSectionCounts();
    if (!counterEl) return;
    var n = articles().filter(function (a) {
      return a.style.display !== "none" && isNew(a) && !a.closest(".gaceta-closed");
    }).length;
    counterEl.textContent = tr("count.new", { n: n });
    counterEl.style.display = n ? "" : "none";
  }

  function topBar() {
    topbar = document.createElement("div");
    topbar.className = "gaceta-topbar";
    counterEl = document.createElement("span");
    counterEl.className = "gaceta-count";
    counterEl.setAttribute("aria-live", "polite");
    topbar.appendChild(counterEl);
    seenToggle = document.createElement("button");
    seenToggle.type = "button";
    seenToggle.textContent = tr("seen.show");
    seenToggle.setAttribute("aria-label", tr("seen.aria"));
    seenToggle.setAttribute("aria-pressed", "false");
    seenToggle.addEventListener("click", function () {
      showSeen = !showSeen;
      seenToggle.textContent = showSeen ? tr("seen.hide") : tr("seen.show");
      seenToggle.setAttribute("aria-pressed", showSeen ? "true" : "false");
      arrangeSections();
    });
    topbar.appendChild(seenToggle);
    topbar.hidden = true;
    document.body.appendChild(topbar);
    var lastY = window.scrollY;
    window.addEventListener("scroll", function () {
      var y = window.scrollY;
      if (Math.abs(y - lastY) < 40) return;
      topbar.classList.toggle("is-away", y > 300 && y > lastY);
      lastY = y;
    }, { passive: true });
  }

  /* ------------------------------------------------------------ reaction bar */

  function syncBar(bar, kinds) {
    var btns = bar.querySelectorAll('button[data-kind="more"],button[data-kind="less"]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].setAttribute("aria-pressed", kinds.indexOf(btns[i].dataset.kind) >= 0 ? "true" : "false");
    }
  }

  function dim(article, kinds) {
    var d = kinds.some(function (k) { return DIM[k]; });
    article.style.transition = "opacity .25s";
    article.style.opacity = d ? ".45" : "";
  }

  function react(kind, storyId) {
    return post("/api/react", {
      reader: reader, date: date, story_id: storyId, kind: kind, by: by
    });
  }

  /* Inline SVG icons (stroke = currentColor), 20px. */
  var ICON = {
    more: '<path d="M12 19V5M5 12l7-7 7 7"/>',
    less: '<path d="M12 5v14M5 12l7 7 7-7"/>',
    seen: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    save: '<path d="M7 4h10v16l-5-3.6L7 20z"/>',
    agent: '<path d="M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4z"/><path d="M19 16v4M17 18h4"/>',
    info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.6v.4"/>'
  };
  function icon(name) {
    return '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      ICON[name] + "</svg>";
  }
  function segment(bar, kind, label, title) {
    var b = document.createElement("button");
    b.type = "button";
    b.dataset.kind = kind;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.innerHTML = icon(kind) + '<span class="gtx"></span>';
    b.querySelector(".gtx").textContent = label;
    bar.appendChild(b);
    return b;
  }

  function buildBar(article, storyId, state, reasons) {
    var bar = document.createElement("div");
    bar.className = "gaceta-rx";
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", tr("bar.aria"));
    function verdict(kind, label, title) {
      var b = segment(bar, kind, label, title);
      b.setAttribute("aria-pressed", "false");
      b.addEventListener("click", function () {
        if (!by) return;
        var opening = b.getAttribute("aria-pressed") !== "true";
        if (opening) b.setAttribute("aria-pressed", "true");
        react(kind, storyId).then(function (res) {
          if (!res) { syncBar(bar, []); toast(tr("err.save")); return; }
          syncBar(bar, res.state.kinds || []);
          /* Decision 12: one 3-way verdict; every option closes the story. */
          if (opening && isNew(article)) {
            markSeen([{ id: storyId, rev: revOf(article), article: article }], kind);
          }
        });
      });
      return b;
    }
    verdict("more", tr("verdict.more"), tr("verdict.more_title"));
    verdict("less", tr("verdict.less"), tr("verdict.less_title"));
    var seen = segment(bar, "seen", tr("verdict.seen"), tr("verdict.seen_title"));
    seen.setAttribute("aria-pressed", "false");
    seen.addEventListener("click", function () {
      if (!by) return;
      if (isSeenNow(article)) {
        seen.setAttribute("aria-pressed", "false");
        markUnseen(article);
      } else {
        seen.setAttribute("aria-pressed", "true");
        markSeen([{ id: storyId, rev: revOf(article), article: article }], "seen");
      }
    });
    if (by) {  /* saves are local and per person (DECISIONS #14) */
      var sv = segment(bar, "save", tr("save"), tr("save.title"));
      sv.setAttribute("aria-pressed", savedState[storyId] ? "true" : "false");
      sv.addEventListener("click", function () {
        var on = sv.getAttribute("aria-pressed") !== "true";
        sv.setAttribute("aria-pressed", on ? "true" : "false");
        post(on ? "/api/save" : "/api/unsave", { reader: reader, story_id: storyId, by: by })
          .then(function (res) {
            if (!res || !res.ok) {
              sv.setAttribute("aria-pressed", on ? "false" : "true");
              toast(tr("err.save"));
              return;
            }
            if (on) savedState[storyId] = 1; else delete savedState[storyId];
            if (on) toast(tr("save.done"));
          });
      });
    }
    if (agentName) {
      var ag = segment(bar, "agent", agentName, tr("agent.title", { agent: agentName }));
      ag.setAttribute("aria-expanded", "false");
      ag.addEventListener("click", function () {
        var open = ag.getAttribute("aria-expanded") !== "true";
        ag.setAttribute("aria-expanded", open ? "true" : "false");
        toggleComposer(article, storyId, bar, ag, open);
      });
    }
    var info = segment(bar, "info", "", tr("info.title"));
    info.className = "gaceta-info";
    info.setAttribute("aria-expanded", "false");
    info.addEventListener("click", function () {
      var open = info.getAttribute("aria-expanded") !== "true";
      info.setAttribute("aria-expanded", open ? "true" : "false");
      togglePanel(article, storyId, reasons, open);
    });
    bar.appendChild(info);
    (article.querySelector(".story-body") || article).appendChild(bar);
    syncBar(bar, state);
    seen.setAttribute("aria-pressed", isSeenNow(article) ? "true" : "false");
    return bar;
  }

  var agentName = null;
  var savedState = {};  /* set from /api/state when the owner has an agent configured */

  function toggleComposer(article, storyId, bar, btn, show) {
    var box = article.querySelector(".gaceta-compose");
    if (box) box.remove();
    if (!show) return;
    box = document.createElement("form");
    box.className = "gaceta-compose";
    var ta = document.createElement("textarea");
    ta.rows = 2;
    ta.maxLength = 1000;
    ta.placeholder = tr("compose.placeholder");
    ta.setAttribute("aria-label", tr("compose.aria", { agent: agentName }));
    var row = document.createElement("div");
    row.className = "gaceta-compose-row";
    var send = document.createElement("button");
    send.type = "submit";
    send.className = "gaceta-compose-send";
    send.textContent = tr("compose.send", { agent: agentName });
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "gaceta-compose-cancel";
    cancel.textContent = tr("compose.cancel");
    cancel.addEventListener("click", function () {
      btn.setAttribute("aria-expanded", "false");
      box.remove();
    });
    row.appendChild(cancel);
    row.appendChild(send);
    box.appendChild(ta);
    box.appendChild(row);
    box.addEventListener("submit", function (ev) {
      ev.preventDefault();
      send.disabled = true;
      post("/api/agent", { reader: reader, story_id: storyId, by: by, note: ta.value })
        .then(function (res) {
          if (!res || !res.ok) {
            send.disabled = false;
            toast(tr("err.send"));
            return;
          }
          var done = document.createElement("p");
          done.className = "gaceta-compose-done";
          done.setAttribute("role", "status");
          done.textContent = res.delivered
            ? tr("agent.sent", { agent: agentName })
            : tr("agent.saved", { agent: agentName });
          box.replaceWith(done);
          btn.setAttribute("aria-expanded", "false");
          btn.setAttribute("aria-pressed", "true");
        });
    });
    bar.parentNode.insertBefore(box, bar.nextSibling);
    ta.focus({ preventScroll: true });
  }

  function togglePanel(article, storyId, reasons, show) {
    var panel = article.querySelector(".gaceta-why");
    if (panel && !show) { panel.remove(); return; }
    if (panel) { panel.remove(); panel = null; }
    panel = document.createElement("div");
    panel.className = "gaceta-why";
    var head = document.createElement("strong");
    head.textContent = tr("why.head");
    panel.appendChild(head);
    var ul = document.createElement("ul");
    (reasons && reasons.length ? reasons : [tr("why.none")]).forEach(function (r) {
      var li = document.createElement("li");
      li.textContent = r;
      ul.appendChild(li);
    });
    panel.appendChild(ul);
    var bar = article.querySelector(".gaceta-rx");
    bar.parentNode.insertBefore(panel, bar.nextSibling);
  }

  /* ------------------------------------------------------------ who reads */

  function byBar() {
    fetch("/api/readers?reader=" + encodeURIComponent(reader))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (info) {
        if (!info) return;
        var bar = document.createElement("div");
        bar.className = "gaceta-bybar";
        var label = document.createElement("span");
        label.textContent = tr("who.label");
        bar.appendChild(label);
        var names = [info.owner].concat(info.household || []);
        names.forEach(function (n) {
          var b = document.createElement("button");
          b.type = "button";
          b.textContent = n;
          b.addEventListener("click", function () {
            try { localStorage.setItem("gaceta.by", n); } catch (e) {}
            location.reload();
          });
          bar.appendChild(b);
        });
        var other = document.createElement("button");
        other.type = "button";
        other.textContent = tr("who.other");
        other.addEventListener("click", function () {
          var input = document.createElement("input");
          input.maxLength = 40;
          input.placeholder = tr("who.name");
          input.setAttribute("aria-label", tr("who.name"));
          other.hidden = true;
          bar.appendChild(input);
          input.focus();
          var save = function () {
            var v = input.value.trim();
            if (!v) return;
            /* 'alice' typed on a new device is the same person as 'Alice': seen
             * marks are keyed by the exact name, so reuse the listed spelling */
            names.forEach(function (n) { if (n && n.toLowerCase() === v.toLowerCase()) v = n; });
            try { localStorage.setItem("gaceta.by", v); } catch (e) {}
            location.reload();
          };
          input.addEventListener("keydown", function (ev) { if (ev.key === "Enter") save(); });
          input.addEventListener("blur", save);
        });
        bar.appendChild(other);
        document.body.appendChild(bar);
      });
  }

  /* ------------------------------------------------------------ cross-device sync
   * The server is the only truth (seen, verdicts, saves per person). A page left
   * open on the PC must catch up with what the phone did meanwhile, so state is
   * re-read when the tab comes back (visibility, focus, bfcache, network back)
   * and every 90 s while visible. A snapshot that raced a local write is dropped.
   * A newer edition is announced ('Edición nueva'), never swapped in under the
   * reader. If the state can't be read, say so instead of silently showing
   * every story as new. */

  var booted = false, booting = false, bootTries = 0;
  var lastSync = 0, baseRevs = null, freshEl = null;
  var SYNC_EVERY = 90000, SYNC_GAP = 15000;

  function fetchState() {
    return fetch("/api/state?reader=" + encodeURIComponent(reader) +
                 (date ? "&date=" + date : "") + "&by=" + encodeURIComponent(by),
                 { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  function readSaved(st) {
    var m = {};
    (st.saved || []).forEach(function (id) { m[id] = 1; });
    return m;
  }

  function sameMap(a, b) {
    var k;
    for (k in a) if (a[k] !== b[k]) return false;
    for (k in b) if (!(k in a)) return false;
    return true;
  }

  /* first visible article below the top edge that stays visible after the sync:
   * hiding stories above it must not move what the reader is looking at */
  function anchorFor(nextSeen) {
    var arts = articles();
    for (var i = 0; i < arts.length; i++) {
      var a = arts[i];
      if (a.style.display === "none" || a.closest(".gaceta-closed")) continue;
      if (nextSeen[a.id] && nextSeen[a.id] === revOf(a)) continue;
      var top = a.getBoundingClientRect().top;
      if (top >= 0) return { el: a, top: top };
    }
    return null;
  }

  function applyRemote(st) {
    var nextSeen = st.seen || {};
    var nextSaved = readSaved(st);
    var arts = articles();
    var changed = !sameMap(seenState, nextSeen) || !sameMap(savedState, nextSaved);
    arts.forEach(function (a) {
      var bar = a.querySelector(".gaceta-rx");
      if (!bar) return;
      var kinds = (st.reactions && st.reactions[a.id]) || [];
      var cur = [].map.call(bar.querySelectorAll('button[aria-pressed="true"][data-kind="more"],' +
        'button[aria-pressed="true"][data-kind="less"]'), function (b) { return b.dataset.kind; });
      if (cur.sort().join() !== kinds.filter(function (k) {
        return k === "more" || k === "less";
      }).sort().join()) { changed = true; syncBar(bar, kinds); }
    });
    if (changed) {
      var anchor = anchorFor(nextSeen);
      arts.forEach(function (a) {  /* un-seen on another device: bring it back */
        if (isSeenNow(a) && nextSeen[a.id] !== revOf(a)) {
          a.style.display = "";
          a.style.opacity = "";
        }
      });
      seenState = nextSeen;
      seenAt = st.seen_at || {};
      savedState = nextSaved;
      arts.forEach(function (a) {
        applyBarSeen(a);
        var sv = a.querySelector('.gaceta-rx button[data-kind="save"]');
        if (sv) sv.setAttribute("aria-pressed", savedState[a.id] ? "true" : "false");
      });
      arrangeSections();
      if (anchor && anchor.el.style.display !== "none") {
        var dy = anchor.el.getBoundingClientRect().top - anchor.top;
        if (Math.abs(dy) > 1) window.scrollBy(0, dy);
      }
    }
    freshCheck(st.revs);
  }

  /* the edition behind this page was rebuilt with new or changed stories */
  function freshCheck(revs) {
    if (!revs || !baseRevs || !topbar || freshEl) return;
    if (sameMap(baseRevs, revs)) return;
    freshEl = document.createElement("button");
    freshEl.type = "button";
    freshEl.className = "gaceta-fresh";
    freshEl.textContent = tr("fresh");
    freshEl.setAttribute("aria-label", tr("fresh.aria"));
    freshEl.addEventListener("click", function () { location.reload(); });
    topbar.insertBefore(freshEl, topbar.firstChild);
    topbar.classList.remove("is-away");
  }

  function sync(force) {
    if (!by || document.visibilityState === "hidden") return;
    if (!booted) { boot(); return; }
    var now = Date.now();
    if (now - lastSync < (force ? 2000 : SYNC_GAP)) return;
    lastSync = now;
    var epoch = writeEpoch;
    fetchState().then(function (st) {
      if (!st || inflight || epoch !== writeEpoch) return;  /* raced a local write */
      applyRemote(st);
    });
  }

  function boot() {
    if (booted || booting) return;
    booting = true;
    fetchState().then(function (st) {
      booting = false;
      if (booted) return;
      if (!st) {
        bootTries++;
        if (bootTries === 1) toast(tr("boot.retry"));
        if (bootTries < 4) setTimeout(boot, 4000 * bootTries);
        else toast(tr("boot.offline"));
        return;
      }
      booted = true;
      lastSync = Date.now();
      baseRevs = st.revs || null;
      seenState = st.seen || {};
      seenAt = st.seen_at || {};
      savedState = readSaved(st);
      agentName = (st.agent && st.owner && by &&
        by.toLowerCase() === String(st.owner).toLowerCase()) ? st.agent : null;
      topbar.hidden = false;
      setupAccordions();
      [].slice.call(document.querySelectorAll("a.saved-link")).forEach(function (l) {
        l.setAttribute("href", "guardadas?by=" + encodeURIComponent(by));
      });
      var arts = articles();
      for (var i = 0; i < arts.length; i++) {
        var a = arts[i];
        var sid = a.id;
        var kinds = (st.reactions && st.reactions[sid]) || [];
        var reasons = (st.reasons && st.reasons[sid]) || [];
        buildBar(a, sid, kinds, reasons);
      }
      sourceVerdicts(st);
      arrangeSections();
    });
  }

  /* ------------------------------------------------------------ main */

  function main() {
    css();
    if (!by) { byBar(); return; }
    topBar();
    boot();
    document.addEventListener("visibilitychange", function () { sync(true); });
    window.addEventListener("focus", function () { sync(false); });
    window.addEventListener("online", function () { sync(true); });
    window.addEventListener("pageshow", function (e) { if (e.persisted) sync(true); });
    setInterval(function () { sync(false); }, SYNC_EVERY);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", main);
  } else {
    main();
  }
})();
