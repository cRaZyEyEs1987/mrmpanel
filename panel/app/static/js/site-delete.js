(function () {
  function confirmSiteDelete(form) {
    var domain = form.getAttribute("data-domain") || "this site";
    var dbName = form.getAttribute("data-db-name") || "";
    var dbEngine = form.getAttribute("data-db-engine") || "";
    var message =
      "Delete " +
      domain +
      "?\n\nThe website files will be permanently deleted.";
    if (!window.confirm(message)) {
      return false;
    }

    form.querySelectorAll('input[name="delete_db"]').forEach(function (el) {
      el.remove();
    });

    if (dbName) {
      var drop = window.confirm(
        "Also delete the linked database " +
          dbName +
          (dbEngine ? " (" + dbEngine + ")" : "") +
          "?\n\nChoose OK to delete the database, or Cancel to keep it."
      );
      if (drop) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = "delete_db";
        input.value = "1";
        form.appendChild(input);
      }
    }
    return true;
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.hasAttribute("data-site-delete")) return;
    if (!confirmSiteDelete(form)) {
      event.preventDefault();
    }
  });
})();
