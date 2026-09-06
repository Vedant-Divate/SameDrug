/* SameDrug progressive enhancement — vanilla, no dependencies. Without JS:
 * theme follows the OS, copy/theme buttons stay hidden, no suggestions
 * (plain form submit is the path); all content is server-rendered. */
(function () {
  "use strict";
  var root = document.documentElement;
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function storedTheme() {
    try { return localStorage.getItem("samedrug-theme"); } catch (e) { return null; }
  }
  function applyTheme(name) {
    if (name) root.setAttribute("data-theme", name);
    else root.removeAttribute("data-theme");
  }
  /* 1. Dark-mode toggle (localStorage override; OS preference otherwise). */
  applyTheme(storedTheme());
  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.hidden = false;
    btn.setAttribute("aria-pressed", String(root.getAttribute("data-theme") === "dark"));
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      try { localStorage.setItem("samedrug-theme", next); } catch (e) { /* private mode */ }
      applyTheme(next);
      btn.setAttribute("aria-pressed", String(next === "dark"));
    });
  });
  /* 2. Copy-link buttons (hidden without JS). */
  document.querySelectorAll("[data-copy-link]").forEach(function (btn) {
    btn.hidden = false;
    var label = btn.textContent;
    function done(ok) {
      btn.textContent = ok ? "Copied" : "Copy failed";
      setTimeout(function () { btn.textContent = label; }, 1500);
    }
    btn.addEventListener("click", function () {
      var url = window.location.href;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(function () { done(true); }, function () { done(false); });
      } else {
        var ta = document.createElement("textarea");
        ta.value = url;
        document.body.appendChild(ta);
        ta.select();
        try { done(document.execCommand("copy")); } catch (e) { done(false); }
        document.body.removeChild(ta);
      }
    });
  });
  /* 3. Live suggestions: debounced 250ms fetch to /api/search. */
  document.querySelectorAll("input[data-suggest]").forEach(function (input) {
    var box = document.createElement("div");
    box.className = "suggest-list";
    box.id = input.getAttribute("aria-controls") || "search-suggest-list";
    box.setAttribute("role", "listbox");
    box.hidden = true;
    input.parentNode.insertBefore(box, input.nextSibling);
    var timer = null, items = [], active = -1;
    function close() {
      box.hidden = true; box.innerHTML = ""; items = []; active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }
    function open(results) {
      box.innerHTML = ""; items = results.slice(0, 8); active = -1;
      items.forEach(function (r, i) {
        var opt = document.createElement("div");
        opt.className = "suggest-item"; opt.id = box.id + "-" + i;
        opt.setAttribute("role", "option");
        opt.textContent = r.molecules + " " + r.strength_set + " " + r.form + (r.has_equivalents ? " (" + r.n_equivalents + ")" : "");
        opt.addEventListener("mousedown", function (ev) {
          ev.preventDefault(); input.value = r.molecules; input.form.submit();
        });
        box.appendChild(opt);
      });
      box.hidden = items.length === 0;
      input.setAttribute("aria-expanded", String(items.length > 0));
    }
    function highlight(i) {
      if (!items.length) return;
      active = (i + items.length) % items.length;
      Array.prototype.forEach.call(box.children, function (child, j) {
        child.classList.toggle("active", j === active);
      });
      input.setAttribute("aria-activedescendant", box.id + "-" + active);
    }
    input.addEventListener("input", function () {
      clearTimeout(timer);
      var q = input.value.trim();
      if (q.length < 2) { close(); return; }
      timer = setTimeout(function () {
        fetch(input.getAttribute("data-suggest") + "?q=" + encodeURIComponent(q) + "&limit=8")
          .then(function (resp) { return resp.ok ? resp.json() : null; })
          .then(function (data) { if (data && data.results) open(data.results); })
          .catch(function () { close(); });
      }, 250);
    });
    input.addEventListener("keydown", function (ev) {
      if (box.hidden) return;
      if (ev.key === "ArrowDown") { ev.preventDefault(); highlight(active + 1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); highlight(active - 1); }
      else if (ev.key === "Enter" && active >= 0) {
        ev.preventDefault(); input.value = items[active].molecules; input.form.submit();
      } else if (ev.key === "Escape") close();
    });
    document.addEventListener("click", function (ev) {
      if (!box.contains(ev.target) && ev.target !== input) close();
    });
  });
  /* 4. Smooth-scroll guard: in-page anchors only, off under reduced motion. */
  if (!reduceMotion) {
    document.querySelectorAll('a[href^="#"]').forEach(function (a) {
      a.addEventListener("click", function (ev) {
        var target = document.querySelector(a.getAttribute("href"));
        if (target) { ev.preventDefault(); target.scrollIntoView({ behavior: "smooth" }); }
      });
    });
  }
})();
