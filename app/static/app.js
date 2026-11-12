// outlook-mailer UI script. Loaded on every admin page. Everything must also work without it.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  // Mapping page: changing the status column reloads the page to list that column's values
  // (the same as pressing "Show values").
  const status = document.getElementById("status");
  const refresh = document.getElementById("status-refresh");
  if (status && refresh && refresh.form) {
    status.addEventListener("change", () => refresh.form.requestSubmit(refresh));
  }

  // Compose page: changing the preview recipient or colours swaps the preview frames' addresses
  // instead of reloading the page, so unsaved edits stay. Without JS the "Show" button reloads.
  const preview = document.getElementById("preview-form");
  if (preview) {
    const r = document.getElementById("preview-r");
    const scheme = document.getElementById("preview-scheme");
    const update = () => {
      const query = new URLSearchParams({ r: r.value, scheme: scheme.value }).toString();
      document.getElementById("preview-html").src = `${preview.dataset.htmlBase}?${query}`;
      document.getElementById("preview-text").src = `${preview.dataset.textBase}?${query}`;
      document.getElementById("compose-r").value = r.value;
      document.getElementById("compose-scheme").value = scheme.value;
      history.replaceState(null, "", `?${query}#preview`);
    };
    r.addEventListener("change", update);
    scheme.addEventListener("change", update);
    preview.addEventListener("submit", (event) => {
      event.preventDefault();
      update();
    });
    document.getElementById("preview-show").hidden = true;
  }

  // Compose page: say when the form has changes the preview doesn't show yet.
  const form = document.getElementById("compose-form");
  const note = document.getElementById("dirty-note");
  if (form && note) {
    form.addEventListener("input", () => { note.hidden = false; });
  }

  // Send progress page: poll the JSON endpoint and update the numbers in place. When the state
  // changes (paused, finished...) reload, so the controls match. Without JS: the Refresh link.
  const progress = document.getElementById("campaign-progress");
  if (progress && !progress.dataset.final) {
    const seconds = Number(progress.dataset.poll) || 3;
    const note = document.getElementById("live-note");
    if (note) note.hidden = false;
    const setField = (name, value) => {
      for (const el of progress.querySelectorAll(`[data-field="${name}"]`)) {
        el.textContent = value == null ? "" : String(value);
      }
      for (const el of progress.querySelectorAll(`[data-show="${name}"]`)) {
        el.hidden = value == null || value === "";
      }
    };
    let timer = null;
    const poll = async () => {
      let data;
      try {
        const response = await fetch(progress.dataset.url, {
          headers: { Accept: "application/json" },
          cache: "no-store",
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        data = await response.json();
      } catch (err) {
        clearInterval(timer);
        if (note) note.textContent = " · live updates stopped; use Refresh";
        return;
      }
      if (data.status !== progress.dataset.status) {
        clearInterval(timer);
        location.reload();
        return;
      }
      for (const name of ["status_label", "done", "percent", "next_send_at", "throttled_until", "resume_at", "last_error"]) {
        setField(name, data[name]);
      }
      const bar = document.getElementById("progress-bar");
      if (bar) bar.value = data.done;
      for (const [status, count] of Object.entries(data.counts)) {
        const cell = progress.querySelector(`[data-count="${status}"]`);
        if (cell) cell.textContent = String(count);
      }
      const events = document.getElementById("events");
      if (events) {
        events.replaceChildren(...data.events.map((e) => {
          const li = document.createElement("li");
          const at = document.createElement("span");
          at.className = "muted";
          at.textContent = e.at;
          li.append(at, ` ${e.message}`);
          return li;
        }));
      }
    };
    timer = setInterval(poll, seconds * 1000);
  }

  // Account page while signing in: count the code's time down, poll the status JSON and reload
  // when the state changes (connected, failed, expired). Without JS: the Refresh link.
  const account = document.getElementById("account-state");
  if (account && account.dataset.state === "signing_in") {
    const seconds = Number(account.dataset.poll) || 3;
    const live = document.getElementById("account-live");
    if (live) live.hidden = false;
    const left = document.getElementById("time-left");
    let remaining = Number(account.dataset.secondsLeft) || 0;
    const countdown = setInterval(() => {
      remaining = Math.max(0, remaining - 1);
      if (left) left.textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;
    }, 1000);
    const timer = setInterval(async () => {
      try {
        const response = await fetch(account.dataset.url, {
          headers: { Accept: "application/json" },
          cache: "no-store",
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (data.state !== account.dataset.state) {
          clearInterval(timer);
          clearInterval(countdown);
          location.reload();
          return;
        }
        if (typeof data.seconds_left === "number") remaining = data.seconds_left;
      } catch (err) {
        clearInterval(timer);
        if (live) live.textContent = " · live updates stopped; use Refresh";
      }
    }, seconds * 1000);
  }

  // Copy buttons (the device code). Shown only when the clipboard API is available.
  for (const button of document.querySelectorAll("button[data-copy]")) {
    const source = document.getElementById(button.dataset.copy);
    if (!source || !navigator.clipboard) continue;
    button.hidden = false;
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(source.textContent.trim());
        button.textContent = "Copied";
      } catch (err) {
        button.textContent = "Copy failed: select the code";
      }
    });
  }
});
