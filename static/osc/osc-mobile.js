(function () {
    const mobileQuery = window.matchMedia("(max-width: 760px)");

    function setMenu(open) {
        document.body.classList.toggle("paperclip-menu-open", open);
        const btn = document.getElementById("paperclipMobileMenuBtn");
        if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function closeMenuOnMobile() {
        if (mobileQuery.matches) setMenu(false);
    }

    function bindMobileMenu() {
        const btn = document.getElementById("paperclipMobileMenuBtn");
        if (btn) {
            btn.addEventListener("click", () => {
                setMenu(!document.body.classList.contains("paperclip-menu-open"));
            });
        }

        document.querySelectorAll("#paperclipNav .tab-btn[data-tab], #paperclipNav a.tab-btn").forEach((tab) => {
            tab.addEventListener("click", closeMenuOnMobile);
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") closeMenuOnMobile();
        });

        mobileQuery.addEventListener?.("change", () => {
            if (!mobileQuery.matches) setMenu(false);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bindMobileMenu);
    } else {
        bindMobileMenu();
    }
})();
