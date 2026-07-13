(function () {
    if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/mobile/sw.js", { scope: "/mobile" }).catch(function () {});
    }
})();
