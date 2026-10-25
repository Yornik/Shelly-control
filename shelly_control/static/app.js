// The chart readout (pointer and arrow keys), focus on a form's errors, and a
// quiet reload of a page left open. Everything the readout shows is also in
// each chart's table view, so the page works without this file. Text goes in
// with textContent only.
(function () {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    node.textContent = text;
    return node;
  }

  function setup(wrap) {
    var svg = wrap.querySelector("svg.viz");
    var readout = wrap.querySelector(".readout");
    var live = wrap.querySelector(".live");
    var cross = svg && svg.querySelector(".cross");
    var hits = svg ? Array.prototype.slice.call(svg.querySelectorAll(".hit")) : [];
    var span = parseFloat(wrap.dataset.span);
    var n = hits.length;
    if (!n || !readout || !cross || !span) return;
    // x is minutes since local midnight; a slot is [x, x + width).
    var slots = hits.map(function (hit) {
      var x = parseFloat(hit.getAttribute("x"));
      return { x: x, end: x + parseFloat(hit.getAttribute("width")), data: hit.dataset };
    });
    // The readout replaces the native tooltips, so they would only double up.
    hits.forEach(function (hit) {
      var title = hit.querySelector("title");
      if (title) hit.removeChild(title);
    });
    var index = -1;

    // The slot under a point of the day, or none in a stretch without prices.
    function slotAt(minute) {
      for (var i = 0; i < n; i++) {
        if (minute >= slots[i].x && minute < slots[i].end) return i;
      }
      return -1;
    }

    function show(i, announce) {
      index = Math.max(0, Math.min(n - 1, i));
      var slot = slots[index];
      var data = slot.data;
      var middle = (slot.x + slot.end) / 2 / span;
      cross.setAttribute("x1", middle * 100 + "%");
      cross.setAttribute("x2", middle * 100 + "%");
      cross.setAttribute("visibility", "visible");
      var plan = "Planned " + data.plan.toLowerCase();
      readout.replaceChildren(
        el("strong", "", data.price),
        el("span", "r-time", data.time),
        el("span", "r-plan" + (data.plan === "On" ? " on" : ""), plan),
        el("span", "r-why", data.why)
      );
      readout.hidden = false;
      // Beside the crosshair, not over it, so the quarter itself stays in view.
      var width = svg.getBoundingClientRect().width;
      var px = middle * width;
      var box = readout.offsetWidth;
      var left = px + 14 + box <= width ? px + 14 : px - 14 - box;
      // CSSOM, not a style attribute: allowed under the page's CSP.
      readout.style.left = Math.max(0, left) + "px";
      if (announce && live) {
        live.textContent = data.time + ": " + data.price + ". " + plan + ". " + data.why;
      }
    }

    function hide() {
      cross.setAttribute("visibility", "hidden");
      readout.hidden = true;
    }

    svg.addEventListener("pointermove", function (e) {
      var box = svg.getBoundingClientRect();
      var i = slotAt(((e.clientX - box.left) / box.width) * span);
      if (i < 0) hide();
      else show(i, false);
    });
    svg.addEventListener("pointerleave", hide);
    wrap.addEventListener("focus", function () {
      show(index >= 0 ? index : parseInt(wrap.dataset.start || "0", 10), true);
    });
    wrap.addEventListener("blur", hide);
    wrap.addEventListener("keydown", function (e) {
      var step = { ArrowRight: 1, ArrowLeft: -1, PageDown: 4, PageUp: -4 }[e.key];
      if (step) {
        show(index + step, true);
      } else if (e.key === "Home") {
        show(0, true);
      } else if (e.key === "End") {
        show(n - 1, true);
      } else if (e.key === "Escape") {
        hide();
        return;
      } else {
        return;
      }
      e.preventDefault();
    });
  }

  document.querySelectorAll(".plotwrap").forEach(setup);

  // A form that was refused: move to its errors, so a phone shows them. The
  // form's #fragment already names them; this covers a browser that only
  // scrolls there. After "load", when the fragment has been handled.
  var summary = document.querySelector(".error-summary");
  if (summary) {
    window.addEventListener("load", function () {
      summary.focus();
    });
  }

  // A page left open shows an old decision after a while. Reload it every five
  // minutes, but only the plain page, and never while someone is using it: a
  // form being filled in, a table or explanation opened, the chart explored.
  var KEY = "reload-scroll";
  try {
    var saved = sessionStorage.getItem(KEY);
    if (saved !== null) {
      sessionStorage.removeItem(KEY);
      window.scrollTo(0, parseInt(saved, 10) || 0);
    }
  } catch (err) {
    // No storage (private window, blocked site data): the reload starts at the top.
  }
  var loaded = Date.now();
  var dirty = false;
  document.addEventListener("input", function () {
    dirty = true;
  });
  function busy() {
    var active = document.activeElement;
    if (active && active !== document.body && active !== document.documentElement) return true;
    if (document.querySelector("details[open]")) return true;
    var readouts = document.querySelectorAll(".readout");
    for (var i = 0; i < readouts.length; i++) {
      if (!readouts[i].hidden) return true;
    }
    return false;
  }
  setInterval(function () {
    // After a refused form the address is /settings or /override, which only take POST.
    if (window.location.pathname !== "/") return;
    if (dirty || busy() || document.visibilityState !== "visible") return;
    if (Date.now() - loaded < 300000) return;
    try {
      sessionStorage.setItem(KEY, String(Math.round(window.scrollY)));
    } catch (err) {
      // See above.
    }
    window.location.replace("/");
  }, 30000);
})();
