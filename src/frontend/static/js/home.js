/*
 * Avanyam home page -- progressive enhancement only, the same contract as
 * portal.js: nothing here may become a precondition for reading the page.
 *
 * Two jobs:
 *   1. reveal each section once as it scrolls into view, by adding the class
 *      home.css gates its hidden starting state on,
 *   2. mark which section the in-page navigation is currently pointing at.
 *
 * Deliberately no `scroll` listener. An IntersectionObserver is cheaper, does
 * not fire on every frame, and is the only version of this that survives a
 * browser throttling background tabs.
 *
 * Both jobs bail out to "everything already visible" if the visitor has asked
 * for reduced motion or the browser has no observer, so the worst outcome of
 * this file failing to run is a page with no animation.
 */
(function () {
  "use strict";

  function revealAll(nodes) {
    nodes.forEach(function (node) { node.classList.add("is-in"); });
  }

  // ---- 1. scroll reveals --------------------------------------------------

  var targets = Array.prototype.slice.call(document.querySelectorAll(".hp-reveal"));

  if (targets.length) {
    var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    if (reduced || typeof window.IntersectionObserver !== "function") {
      revealAll(targets);
    } else {
      var revealer = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting) return;
            entry.target.classList.add("is-in");
            // Unobserved after the first pass: a section that fades in every
            // time it is scrolled back past reads as a glitch, not as polish.
            revealer.unobserve(entry.target);
          });
        },
        { rootMargin: "0px 0px -10% 0px", threshold: 0.1 }
      );

      targets.forEach(function (node) { revealer.observe(node); });
    }
  }

  // ---- 2. section marking in the in-page nav ------------------------------

  var links = Array.prototype.slice.call(document.querySelectorAll('.hp-nav a[href^="#"]'));

  if (links.length && typeof window.IntersectionObserver === "function") {
    var byId = {};

    links.forEach(function (link) {
      var target = document.getElementById(link.getAttribute("href").slice(1));
      if (target) byId[target.id] = link;
    });

    var sections = Object.keys(byId);

    if (sections.length) {
      var marker = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting) return;
            links.forEach(function (link) { link.removeAttribute("aria-current"); });
            byId[entry.target.id].setAttribute("aria-current", "true");
          });
        },
        // A band across the middle of the viewport, so the marked link changes
        // where the reader is looking rather than where the page technically is.
        { rootMargin: "-45% 0px -45% 0px" }
      );

      sections.forEach(function (id) { marker.observe(document.getElementById(id)); });
    }
  }
})();