// outlook-mailer UI script. Loaded on every admin page. Everything must also work without it.
"use strict";

// Mapping page: changing the status column reloads the page to list that column's values
// (the same as pressing "Show values").
document.addEventListener("DOMContentLoaded", () => {
  const status = document.getElementById("status");
  const refresh = document.getElementById("status-refresh");
  if (status && refresh && refresh.form) {
    status.addEventListener("change", () => refresh.form.requestSubmit(refresh));
  }
});
