(function () {
  "use strict";

  function initializeTinyMCE() {
    if (typeof window.tinymce === "undefined") {
      return;
    }

    window.tinymce.init({
      selector: "textarea.js-tinymce",
      license_key: "gpl",
      height: 480,
      menubar: false,
      plugins: "autolink code link lists table",
      toolbar:
        "undo redo | blocks | bold italic underline | bullist numlist | " +
        "link table | removeformat | code",
      promotion: false,
      branding: false,
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeTinyMCE);
  } else {
    initializeTinyMCE();
  }
})();
