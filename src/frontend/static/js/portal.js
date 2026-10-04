/*
 * Avanyam portal -- progressive enhancement only.
 *
 * Everything here is an enhancement. The signup, login and approval flows all
 * work with JavaScript disabled, because they are plain server-side form posts.
 * Nothing in this file is allowed to become a precondition for a user
 * completing a task.
 *
 * Three jobs:
 *   1. mark a submit button busy so a double-click cannot create two signup
 *      requests (the unique constraint on SignupRequest.email is the real
 *      guarantee; this just avoids a confusing error message),
 *   2. confirm destructive-ish actions (declining an application) natively,
 *      rather than with a custom modal that would trap keyboard users,
 *   3. live password feedback, because the strength rules are otherwise only
 *      visible after a rejected submit.
 *
 * Two more live at the bottom: the OTP dialog for reversing a decline (3), and
 * the toast countdown (5). The toasts themselves are server-rendered in
 * base.html, so the message and its markup survive with this file blocked --
 * only the draining is added here.
 *
 * Last is the account menu (6), which is a checkbox disclosure in the markup
 * and gains only its Escape and outside-click handling here.
 */
(function () {
  "use strict";

  // ---- 1. busy state ------------------------------------------------------

  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector('button[type="submit"]');
      if (!button) return;
      // No spinner delay: the page is usually fast, and a visible spinner that
      // flashes for 80ms reads as a glitch.
      button.setAttribute("aria-busy", "true");
      button.dataset.originalLabel = button.textContent.trim();
    });
  });

  // ---- 2. confirmation ----------------------------------------------------
  //
  // Replaces the browser's built-in confirm dialog. That one ignored the design
  // system, and it was attached to the form, so a shared prompt had to be worded for
  // whichever action was riskiest -- which meant Approve opened a dialog asking
  // the trainer to confirm a decline. The prompt now belongs to the button that
  // was pressed, and says what that button does.
  //
  // Everything here is an enhancement. With JS off, the buttons submit and the
  // server still enforces the decision; the trainer just loses the prompt.

  (function confirmationDialog() {
    var dialog = document.getElementById("confirm-dialog");
    if (!dialog) return;

    var title = document.getElementById("confirm-title");
    var body = document.getElementById("confirm-body");
    var accept = dialog.querySelector("[data-confirm-accept]");
    var cancel = dialog.querySelector("[data-confirm-cancel]");
    var pending = null;

    function openFor(button) {
      pending = button;
      title.textContent = button.getAttribute("data-confirm") || "Are you sure?";
      body.textContent = button.getAttribute("data-confirm-body") || "";
      accept.textContent = button.getAttribute("data-confirm-label") || "Continue";
      accept.className =
        "btn " +
        (button.getAttribute("data-confirm-tone") === "reject"
          ? "btn--reject-solid"
          : "btn--primary");
      // A one-line body with no note attached reads as a stray sentence.
      body.hidden = !body.textContent;

      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        // No <dialog>.showModal: still our markup, still styled, but we lose
        // focus trapping and Esc handling, so bind Esc ourselves.
        dialog.setAttribute("open", "");
        document.addEventListener("keydown", onFallbackEscape);
        cancel.focus();
      }
    }

    function onFallbackEscape(event) {
      if (event.key === "Escape" && pending) close();
    }

    function close() {
      pending = null;
      document.removeEventListener("keydown", onFallbackEscape);
      if (typeof dialog.close === "function" && dialog.open) {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }

    document.querySelectorAll("button[data-confirm]").forEach(function (button) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        openFor(button);
      });
    });

    cancel.addEventListener("click", close);

    // Esc with showModal() closes the dialog natively; this just drops the
    // pending reference so a later Enter cannot resubmit a stale button.
    dialog.addEventListener("close", function () {
      pending = null;
      document.removeEventListener("keydown", onFallbackEscape);
    });

    // Guard against the dialog being closed by means we did not handle.
    dialog.addEventListener("cancel", function () {
      pending = null;
    });

    accept.addEventListener("click", function () {
      var button = pending;
      close();
      if (!button) return;

      var form = button.form;
      if (!form) return;

      // requestSubmit(button) -- not form.submit().
      // A programmatic form.submit() does not include the pressed button's
      // name/value, so decision would arrive empty and every row would decline.
      // Passing the button through keeps name/value intact, and still fires the
      // submit event so the busy-state handler runs.
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit(button);
        return;
      }

      // Fallback for browsers without requestSubmit: carry the value across
      // explicitly rather than dropping it.
      var name = button.name || "decision";
      var field = form.querySelector('input[name="' + name + '"]');
      if (!field) {
        field = document.createElement("input");
        field.type = "hidden";
        field.name = name;
        form.appendChild(field);
      }
      field.value = button.value;
      form.submit();
    });
  })();

  // ---- 3. undoing a decline ----------------------------------------------
  //
  // One top-centre panel, two steps, in order: confirm, then enter the code.
  // Deliberately sequential. The page used to render two competing controls on
  // every declined row -- a button to send a code, and a permanently visible box
  // captioned "Or enter the code we emailed you" -- which asked the trainer to
  // choose between two things that are consecutive steps, and let them submit an
  // empty box to discover no code had been sent.
  //
  // The panel is not a toast and not a modal. It is not a toast because the code
  // has to be typed into it and it must not expire under the trainer's hands; it
  // is not a modal because undoing a row is done while reading the list of rows,
  // and a centred dialog covers the list they came from. It borrows the toasts'
  // position, palette and arrival so it reads as the same kind of interruption.
  //
  // The rule this implements: a refusal changes nothing. The server only moves the
  // application out of `rejected` while redeeming a code, so every error path here
  // just re-reports the message and stays on whichever step it was on. Cancelling
  // mid-way is the same -- still declined, and a code sent but never used expires
  // on its own.
  //
  // "Code sent" is only shown once the server has said the SMTP server took the
  // message. The send is synchronous for exactly this reason: a queued task could
  // not report failure, and the panel would have been left telling the trainer to
  // check an inbox that was never going to ring.

  (function undoPanel() {
    var panel = document.getElementById("undo-otp");
    if (!panel) return;
    // No fetch means no in-panel posting. The queue button still posts, and lands
    // on the undo page, which completes the same two steps.
    if (typeof window.fetch !== "function") return;

    var box = panel.querySelector(".otp__panel");
    var title = document.getElementById("undo-title");
    var body = document.getElementById("undo-body");
    var error = document.getElementById("undo-error");
    var meta = document.getElementById("undo-countdown");
    var emailOut = document.getElementById("undo-otp-email");
    var clock = document.getElementById("undo-clock");
    var codeBox = document.getElementById("undo-step-code");
    var input = document.getElementById("undo-code-input");
    var accept = panel.querySelector("[data-undo-accept]");
    var cancels = panel.querySelectorAll("[data-undo-cancel]");

    var pending = null; // the button that opened us
    var lastFocus = null;
    var step = "send";
    var busy = false;
    var ticker = null;
    var deadline = null;

    var MS_PER_SECOND = 1000;

    // Read the tick rate back out of CSS rather than repeating it here, for the
    // same reason the toast duration is: the stylesheet decides how long these
    // interactions last, and a second copy in script eventually disagrees.
    function tickMs() {
      var raw = parseFloat(
        window.getComputedStyle(box).getPropertyValue("--undo-tick-ms")
      );
      return isFinite(raw) && raw > 0 ? raw : MS_PER_SECOND;
    }

    function csrfToken() {
      var field = document.querySelector("[name=csrfmiddlewaretoken]");
      return field ? field.value : "";
    }

    function showError(message) {
      error.textContent = message;
      error.hidden = false;
    }

    function clearError() {
      error.textContent = "";
      error.hidden = true;
    }

    function stopTicker() {
      if (ticker !== null) {
        window.clearInterval(ticker);
        ticker = null;
      }
      deadline = null;
      box.classList.remove("is-urgent");
    }

    // The clock is a readout of the server's expiry, captured as a remaining count
    // when it answered. Counting down from a locally started ten minutes would
    // drift from the token and could show 04:00 over a code that died a minute ago.
    function tick() {
      if (deadline === null || !clock) return;
      var left = Math.max(
        0,
        Math.round((deadline - Date.now()) / MS_PER_SECOND)
      );
      var m = Math.floor(left / 60);
      var sec = left % 60;
      clock.textContent = m + ":" + (sec < 10 ? "0" + sec : sec);
      box.classList.toggle("is-urgent", left > 0 && left <= 120);
      if (left === 0) {
        stopTicker();
        showError("This code has expired. Request a new one.");
        if (step === "code") input.focus();
      }
    }

    function startCountdown(seconds) {
      if (!meta || !clock) return;
      meta.hidden = false;
      deadline = Date.now() + Math.max(0, seconds) * MS_PER_SECOND;
      if (ticker !== null) window.clearInterval(ticker);
      tick();
      ticker = window.setInterval(tick, tickMs());
    }

    function setStep(which) {
      step = which;
      codeBox.hidden = which !== "code";
      if (which !== "code") {
        if (meta) meta.hidden = true;
        stopTicker();
      }
      accept.disabled = false;

      if (which === "send") {
        title.textContent = pending.getAttribute("data-name")
          ? "Undo the decline for " + pending.getAttribute("data-name") + "?"
          : "Undo this decline?";
        body.textContent = "We will email you a six-digit code. Enter it to confirm.";
        accept.textContent = "Email me a code";
      } else if (which === "sending") {
        // Shown while the request is in flight. The panel is already on screen by
        // this point, so the wait is legible rather than a frozen page: Gmail's
        // SMTP handshake takes several seconds and pretending otherwise would be
        // the same dishonesty as claiming the mail had gone out early.
        title.textContent = "Sending your code";
        body.textContent =
          "Emailing a six-digit code to " +
          (pending.getAttribute("data-email") || "your address") +
          ". This takes a few seconds.";
        accept.textContent = "Sending…";
        accept.disabled = true;
      } else {
        title.textContent = "Code sent";
        accept.textContent = "Undo decline";
      }
      clearError();
      input.value = "";
    }

    function show() {
      lastFocus = document.activeElement;
      // Unhiding restarts the arrival animation by itself: a CSS animation on an
      // element that goes from `display: none` to visible begins again from the
      // start, so there is no class to toggle and no reflow to force.
      panel.hidden = false;
      document.addEventListener("keydown", onKeydown);
      input.value = "";
    }

    function hide() {
      panel.hidden = true;
      pending = null;
      busy = false;
      stopTicker();
      document.removeEventListener("keydown", onKeydown);
      if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
      lastFocus = null;
    }

    function onKeydown(event) {
      // Escape backs out, as it does in a dialog. Tab is left alone: this is not
      // modal, so the trainer can still reach the rest of the queue behind it, and
      // trapping focus would fight that.
      if (event.key === "Escape" && pending) hide();
    }

    function open(button) {
      pending = button;
      // A live code already exists for this row, and the button said so. Mailing
      // another one would expire the code the trainer is holding, so the panel
      // opens on the code box instead of asking them to request a fresh mail.
      if (button.hasAttribute("data-undo-pending")) {
        var waiting = button.getAttribute("data-email");
        setStep("code");
        show();
        // The countdown strip reads "Sent to <address>", so the address has to be
        // filled in here too -- the send path sets it from the response, and
        // without this the strip showed a bare "Sent to".
        if (emailOut) emailOut.textContent = waiting || "your address";
        body.textContent =
          "We emailed you a six-digit code to " +
          (waiting || "your address") +
          ". Enter it to confirm.";
        startCountdown(parseInt(button.getAttribute("data-seconds-left"), 10) || 0);
        input.focus();
        return;
      }
      setStep("send");
      show();
      accept.focus();
    }

    function post(url, payload) {
      return fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "X-CSRFToken": csrfToken(),
          "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        body: payload,
      }).then(function (response) {
        return response
          .json()
          .catch(function () {
            // A redirect to the login page or an error page arrives as HTML.
            // Nothing was applied, so say so rather than showing a parse error.
            return {
              ok: false,
              error: "The server sent an unexpected reply. Nothing was changed.",
            };
          })
          .then(function (data) {
            return { status: response.status, data: data };
          });
      });
    }

    function restoreAccept() {
      accept.disabled = false;
      accept.textContent = step === "code" ? "Undo decline" : "Email me a code";
    }

    function submit() {
      if (busy || !pending) return;

      var code = input.value.trim();
      if (step === "code" && !code) {
        showError("Enter the six-digit code from the email.");
        input.focus();
        return;
      }

      var url =
        step === "send" ? pending.getAttribute("data-code-url")
                        : pending.getAttribute("data-confirm-url");

      busy = true;
      accept.disabled = true;
      if (step === "send") {
        // Move to the in-flight state immediately, before the request goes out, so
        // the panel reflects the send while it happens rather than after.
        setStep("sending");
      } else {
        accept.textContent = "Checking…";
      }
      clearError();

      post(url, step === "send" ? "" : "code=" + encodeURIComponent(code))
        .then(function (result) {
          busy = false;

          if (!result.data.ok) {
            // Back to the send step, or stay on the code step. Either way the
            // server refused, so the application is still declined and the trainer
            // can retry, correct a typo, or cancel.
            if (step === "sending") step = "send";
            showError(result.data.error || "That did not work. Nothing was changed.");
            restoreAccept();
            if (step === "code") input.focus();
            return;
          }

          if (step === "sending") {
            // The send is synchronous, so this response is a delivery receipt: a
            // failure came back as `ok: false` above and never reached this line.
            setStep("code");
            var address = result.data.email || pending.getAttribute("data-email");
            if (emailOut) emailOut.textContent = address || "your address";
            body.textContent =
              "We emailed a six-digit code to " +
              (address || "your address") +
              ". Enter it to confirm.";
            startCountdown(result.data.seconds_left || 0);
            input.focus();
            return;
          }

          // Redeemed. The row is back in the queue, so the page is now wrong --
          // reload rather than patch one row's markup by hand.
          hide();
          window.location.reload();
        })
        .catch(function () {
          busy = false;
          if (step === "sending") step = "send";
          showError("Could not reach the server. Nothing was changed.");
          restoreAccept();
        });
    }

    document.querySelectorAll("[data-undo-start]").forEach(function (button) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        open(button);
      });
    });

    accept.addEventListener("click", submit);
    Array.prototype.forEach.call(cancels, function (button) {
      button.addEventListener("click", hide);
    });

    // Enter in the code box confirms, as it does in a form. Without this the
    // trainer has to reach for the button.
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
  })();

  // ---- 4. password feedback ----------------------------------------------

  function scorePassword(value) {
    if (!value) return { score: 0, label: "" };

    var score = 0;
    if (value.length >= 12) score++;
    if (value.length >= 16) score++;
    if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score++;
    if (/\d/.test(value)) score++;
    if (/[^\w\s]/.test(value)) score++;

    // A long passphrase of one repeated character is not a strong password just
    // because it is long, so penalise very low character variety.
    if (new Set(value).size < 5) score = Math.min(score, 1);

    var labels = ["", "weak", "fair", "good", "strong", "strong"];
    return { score: score, label: labels[score] };
  }

  var passwordInputs = document.querySelectorAll('input[autocomplete="new-password"]');
  if (passwordInputs.length) {
    var meter = document.createElement("p");
    meter.className = "hint";
    meter.setAttribute("role", "status");
    meter.setAttribute("aria-live", "polite");
    var first = passwordInputs[0];
    if (first && first.parentNode) first.parentNode.appendChild(meter);

    first.addEventListener("input", function () {
      var result = scorePassword(first.value);
      if (!first.value) {
        meter.textContent = "";
        first.parentNode.classList.remove("field--valid", "field--invalid");
        return;
      }
      // The bar is advisory only. Django's AUTH_PASSWORD_VALIDATORS remain the
      // authority -- a client-side "strong" must never be what lets a weak
      // password through.
      meter.textContent = "Strength: " + result.label;
      meter.dataset.score = String(result.score);
      first.parentNode.classList.toggle(
        "field--invalid",
        result.score > 0 && result.score <= 1
      );
      first.parentNode.classList.toggle(
        "field--valid",
        result.score >= 3
      );
    });

    // When the confirm field matches, say so -- it is the single most common
    // form error and costs the user a full round-trip to discover.
    var confirm = passwordInputs[1];
    if (confirm) {
      confirm.addEventListener("input", function () {
        if (!confirm.value) return;
        var match = confirm.value === first.value;
        confirm.parentNode.classList.toggle("field--valid", match);
        confirm.parentNode.classList.toggle("field--invalid", !match);
      });
    }
  }

    // ---- 5. toasts ---------------------------------------------------------
    //
    // Server-rendered, so this block only adds behaviour: drain, dismiss, pause.
    // Two deliberate rules:
    //
    //   * The duration is read back out of the CSS custom property `--toast-ms`
    //     rather than hardcoded here, because the progress bar is animated
    //     against that same value. Two sources would eventually disagree, and the
    //     bar would then lie about how much time is left.
    //   * A toast built from form errors never expires by itself. A message the
    //     user still has to act on should not scroll away because they were slow
    //     to look at it.

    var toastNodes = document.querySelectorAll("[data-toast]");

    function toastDuration(toast) {
      var ms = parseFloat(getComputedStyle(toast).getPropertyValue("--toast-ms"));
      // A missing or malformed value must not disable dismissal entirely.
      return isFinite(ms) && ms > 0 ? ms : 6000;
    }

    function dismiss(toast) {
      if (toast.dataset.leaving) return;
      toast.dataset.leaving = "1";
      toast.classList.add("is-leaving");
      // Backstop for reduced motion, where `is-leaving` has no animation and so
      // would never fire animationend.
      window.setTimeout(function () { toast.remove(); }, 400);
    }

    toastNodes.forEach(function (toast) {
      var close = toast.querySelector("[data-toast-close]");
      if (close) {
        close.addEventListener("click", function () { dismiss(toast); });
      }

      if (toast.hasAttribute("data-toast--persistent")) {
        // Freeze the bar so it reads as "held" rather than "nearly gone".
        toast.classList.add("is-paused");
        return;
      }

      var remaining = toastDuration(toast);
      var startedAt = Date.now();
      var timer = null;

      function start() {
        toast.classList.remove("is-paused");
        startedAt = Date.now();
        timer = window.setTimeout(function () { dismiss(toast); }, remaining);
      }

      function pause() {
        if (timer === null) return;
        window.clearTimeout(timer);
        timer = null;
        remaining -= Date.now() - startedAt;
        toast.classList.add("is-paused");
      }

      function resume() {
        if (timer !== null || remaining <= 0) return;
        start();
      }

      // Hovering or focusing pauses the countdown. Without this a toast is
      // unreachable to anyone who needs longer than the duration to read it, and
      // the close button lives inside the thing that is vanishing.
      toast.addEventListener("mouseenter", pause);
      toast.addEventListener("focusin", pause);
      toast.addEventListener("mouseleave", resume);
      toast.addEventListener("focusout", function (event) {
        // Ignore focus moving within the toast, e.g. onto its own close button.
        if (toast.contains(event.relatedTarget)) return;
        resume();
      });

      start();
    });
})();

// ---- 6. account menu -------------------------------------------------------

// The avatar opens the menu by itself: its control is a checkbox, so the panel
// opens and closes with this file blocked. All that is added here is the three
// ways out that CSS cannot express -- Escape, a click elsewhere, and tabbing
// past the last item -- without which a panel opened with the keyboard has no
// keyboard-only way to be shut again.

(function accountMenu() {
  var menu = document.querySelector(".usermenu");
  if (!menu) return;

  var toggle = menu.querySelector(".usermenu__toggle");
  if (!toggle) return;

  function close(restoreFocus) {
    if (!toggle.checked) return;
    toggle.checked = false;
    // Only Escape sends focus back. Closing because of a click elsewhere must
    // leave focus where the visitor just put it, or the click does nothing.
    if (restoreFocus) toggle.focus();
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") close(true);
  });

  document.addEventListener("click", function (event) {
    // The avatar's label is inside `.usermenu`, so clicking it lands here and
    // is left to the checkbox's own toggle.
    if (!menu.contains(event.target)) close(false);
  });

  menu.addEventListener("focusout", function (event) {
    if (menu.contains(event.relatedTarget)) return;
    close(false);
  });
})();
