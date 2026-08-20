(function (global) {
    "use strict";

    const COOKIE_NAME = "X-CSRF-Token";
    const HEADER_NAME = "X-CSRF-Token";
    const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

    function readCookie(name = COOKIE_NAME) {
        const escaped = String(name).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${escaped}=([^;]+)`));
        return match ? decodeURIComponent(match[1]) : "";
    }

    function withToken(init = {}) {
        const method = String(init.method || "GET").toUpperCase();
        const options = {
            ...init,
            method,
            credentials: init.credentials || "same-origin",
        };
        const headers = new Headers(init.headers || {});
        if (UNSAFE_METHODS.has(method)) {
            const token = readCookie();
            if (token) headers.set(HEADER_NAME, token);
        }
        options.headers = headers;
        return options;
    }

    async function isCsrfFailure(response) {
        if (!response || response.status !== 403) return false;
        try {
            const payload = await response.clone().json();
            return payload && payload.code === "csrf_validation_failed";
        } catch (_) {
            return false;
        }
    }

    async function refreshCookie() {
        await fetch(window.location.href, {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: { "Accept": "text/html" },
        });
        return readCookie();
    }

    async function csrfFetch(input, init = {}) {
        let response = await fetch(input, withToken(init));
        if (!await isCsrfFailure(response)) return response;

        await refreshCookie();
        response = await fetch(input, withToken(init));
        return response;
    }

    global.MAGICsrf = Object.freeze({
        fetch: csrfFetch,
        readCookie,
        withToken,
    });
}(window));
