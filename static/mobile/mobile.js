(function () {
    "use strict";

    const form = document.getElementById("mobileChatForm");
    const input = document.getElementById("mobileChatInput");
    const messages = document.getElementById("mobileChatMessages");
    const sendButton = document.getElementById("mobileSendButton");
    const typing = document.getElementById("typingState");
    const intentStatus = document.getElementById("mobileIntentStatus");
    const heavyButton = document.getElementById("heavyModeButton");
    const clearButton = document.getElementById("clearChatButton");
    const networkState = document.getElementById("networkState");
    let heavyMode = false;
    let busy = false;

    function readCookie(name) {
        const prefix = String(name || "") + "=";
        const part = document.cookie.split("; ").find(function (row) {
            return row.startsWith(prefix);
        });
        if (!part) return "";
        try {
            return decodeURIComponent(part.slice(prefix.length));
        } catch (_error) {
            return part.slice(prefix.length);
        }
    }

    function csrfToken() {
        return readCookie("X-CSRF-Token");
    }

    async function refreshCsrfToken() {
        const response = await fetch("/mobile", {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: { "Accept": "text/html", "X-MAGI-CSRF-Refresh": "1" }
        });
        const finalPath = new URL(response.url, window.location.origin).pathname;
        if (response.status === 401 || finalPath === "/login") return false;
        return response.ok && Boolean(csrfToken());
    }

    async function sendChatRequest(outbound, allowCsrfRetry) {
        const headers = { "Content-Type": "application/json", "Accept": "application/json" };
        const token = csrfToken();
        if (token) headers["X-CSRF-Token"] = token;

        const response = await fetch("/api/osc/chat", {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: headers,
            body: JSON.stringify({ message: outbound })
        });
        const data = await response.json().catch(function () { return {}; });

        if (response.status === 403 && data.code === "csrf_validation_failed" && allowCsrfRetry) {
            const refreshed = await refreshCsrfToken();
            if (refreshed) return sendChatRequest(outbound, false);
            return { response: response, data: { error: "auth_required" } };
        }
        return { response: response, data: data };
    }

    function setNetworkState() {
        if (!networkState) return;
        const online = navigator.onLine;
        const label = networkState.querySelector(".live-state-label");
        networkState.classList.toggle("is-online", online);
        if (label) label.textContent = online ? "已連線" : "離線";
    }

    function resizeInput() {
        if (!input) return;
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 150) + "px";
    }

    function scrollMessages() {
        if (messages) messages.scrollTo({ top: messages.scrollHeight, behavior: "smooth" });
    }

    function appendMessage(role, text, meta, artifacts) {
        if (!messages) return;
        const article = document.createElement("article");
        article.className = "message message-" + role;

        if (role === "assistant") {
            const avatar = document.createElement("div");
            avatar.className = "message-avatar";
            avatar.setAttribute("aria-hidden", "true");
            avatar.textContent = "M";
            article.appendChild(avatar);
        }

        const body = document.createElement("div");
        body.className = "message-body";
        const paragraph = document.createElement("p");
        paragraph.textContent = String(text || "");
        body.appendChild(paragraph);

        if (meta) {
            const detail = document.createElement("div");
            detail.className = "message-meta";
            detail.textContent = meta;
            body.appendChild(detail);
        }

        if (Array.isArray(artifacts) && artifacts.length) {
            const list = document.createElement("div");
            list.className = "artifact-list";
            artifacts.forEach(function (artifact) {
                const url = artifact.download_url || artifact.url || "";
                if (!url) return;
                const link = document.createElement("a");
                link.href = url;
                link.textContent = artifact.label || artifact.name || "下載處理結果";
                list.appendChild(link);
            });
            if (list.childNodes.length) body.appendChild(list);
        }

        article.appendChild(body);
        messages.appendChild(article);
        scrollMessages();
    }

    function routeDescription(intent) {
        if (!intent) return "已完成處理";
        let text = "已辨識：" + (intent.label || "一般對話");
        if (intent.uses_tool) text += " · 會使用資料工具";
        if (intent.heavy) text += " · 深度模式";
        return text;
    }

    function friendlyFailureMessage(error) {
        // Never render server-provided error text here: it can contain an
        // implementation detail or a support/trace identifier.  The user
        // only needs a safe next step, while the server keeps diagnostics.
        if (error && error.message === "csrf_retry_needed") {
            return "安全驗證未能自動更新；登入仍然保留，請再送出一次。";
        }
        if (error && error.message === "permission_denied") {
            return "目前帳號沒有執行這項工作的權限。";
        }
        return "目前無法完成這則訊息，原文已保留在輸入框。請稍後再試。";
    }

    function setBusy(value) {
        busy = value;
        if (sendButton) sendButton.disabled = value;
        if (input) input.disabled = value;
        if (typing) typing.hidden = !value;
        if (value) scrollMessages();
    }

    async function submitMessage(rawMessage) {
        const original = String(rawMessage || "").trim();
        if (!original || busy) return;
        const outbound = heavyMode ? "@heavy " + original : original;
        appendMessage("user", original, heavyMode ? "深度模式" : "");
        input.value = "";
        resizeInput();
        intentStatus.hidden = false;
        intentStatus.textContent = "MAGI 正在辨識工作類型…";
        setBusy(true);

        try {
            const result = await sendChatRequest(outbound, true);
            const response = result.response;
            const data = result.data;
            if (response.status === 401 || data.error === "auth_required") {
                window.location.assign("/login?next=%2Fmobile");
                return;
            }
            if (response.status === 403 && data.code === "csrf_validation_failed") {
                throw new Error("csrf_retry_needed");
            }
            if (response.status === 403) {
                throw new Error(data.error === "admin_required" ? "permission_denied" : "request_forbidden");
            }
            if (!response.ok) throw new Error("request_failed");
            const routeText = routeDescription(data.intent);
            intentStatus.textContent = routeText;
            appendMessage("assistant", data.reply || "MAGI 沒有回傳文字內容。", routeText, data.artifacts);
        } catch (error) {
            intentStatus.textContent = "本次未完成";
            if (!input.value) {
                input.value = original;
                resizeInput();
            }
            appendMessage("assistant", friendlyFailureMessage(error));
        } finally {
            setBusy(false);
            input.focus();
        }
    }

    if (form && input) {
        form.addEventListener("submit", function (event) {
            event.preventDefault();
            submitMessage(input.value);
        });
        input.addEventListener("input", resizeInput);
        input.addEventListener("keydown", function (event) {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                event.preventDefault();
                form.requestSubmit();
            }
        });
    }

    document.querySelectorAll("[data-prompt]").forEach(function (button) {
        button.addEventListener("click", function () {
            input.value = button.dataset.prompt || "";
            resizeInput();
            input.focus();
        });
    });

    if (heavyButton) {
        heavyButton.addEventListener("click", function () {
            heavyMode = !heavyMode;
            heavyButton.setAttribute("aria-pressed", String(heavyMode));
        });
    }

    if (clearButton && messages) {
        clearButton.addEventListener("click", function () {
            messages.replaceChildren();
            appendMessage("assistant", "畫面已清除。你可以開始新的對話。", "");
            intentStatus.hidden = true;
            input.focus();
        });
    }

    document.querySelectorAll(".mobile-bottom-nav a[href^='#']").forEach(function (link) {
        link.addEventListener("click", function () {
            document.querySelectorAll(".mobile-bottom-nav a").forEach(function (item) { item.classList.remove("is-active"); });
            link.classList.add("is-active");
            const target = document.querySelector(link.getAttribute("href"));
            if (target && target.tagName === "DETAILS") target.open = true;
        });
    });

    window.addEventListener("online", setNetworkState);
    window.addEventListener("offline", setNetworkState);
    setNetworkState();
    resizeInput();

    if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/mobile/sw.js", { scope: "/mobile" }).catch(function () {});
    }
})();
