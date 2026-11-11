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
});
