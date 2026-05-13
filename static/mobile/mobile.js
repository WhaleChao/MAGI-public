(function () {
    if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/static/mobile/sw.js").catch(function () {});
    }
})();
