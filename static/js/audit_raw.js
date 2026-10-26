/* Raw text audit console.
 *
 * Sibling of audit.js: a shell page that fills itself from the raw-audit JSON
 * endpoints. Every user-visible string comes from the #i18n blob, and every
 * dynamic value goes in through textContent, never innerHTML.
 *
 * The values here are the most hostile on the whole dashboard: the request
 * prompt and the upstream response body are arbitrary upstream text and may
 * contain markup. They are only ever assigned to textContent, and the two
 * <pre> nodes are the only places they land.
 */
(function () {
    "use strict";

    var I18N = JSON.parse(document.getElementById("i18n").textContent);

    /* Matches audit.js. Slow enough to leave the tab open without hammering
       the audit store, fast enough to watch a request land. */
    var AUTO_REFRESH_MS = 15000;

    var PAGE_SIZE = 50;

    var el = {
        window: document.getElementById("raw-window"),
        refresh: document.getElementById("raw-refresh"),
        status: document.getElementById("raw-status"),
        notice: document.getElementById("raw-notice"),
        list: document.getElementById("raw-list"),
        empty: document.getElementById("raw-empty"),
        detailEmpty: document.getElementById("raw-detail-empty"),
        panes: document.getElementById("raw-panes"),
        prompt: document.getElementById("raw-prompt"),
        response: document.getElementById("raw-response"),
        dlPrompt: document.getElementById("raw-dl-prompt"),
        dlResponse: document.getElementById("raw-dl-response")
    };

    var selectedId = null;
    var promptText = "";
    var responseText = "";
    var timer = null;
    var inFlight = false;

    // ------------------------------------------------------------------ utils

    function clear(node) {
        while (node.firstChild) {
            node.removeChild(node.firstChild);
        }
    }

    function fmtTime(seconds) {
        if (!seconds) {
            return I18N.raw_none;
        }
        var when = new Date(seconds * 1000);
        return isNaN(when.getTime()) ? I18N.raw_none : when.toLocaleString();
    }

    function fmtValue(value) {
        return value === null || value === undefined
            ? I18N.raw_none
            : String(value);
    }

    function showNotice(text, isError) {
        if (!text) {
            el.notice.hidden = true;
            el.notice.textContent = "";
            el.notice.classList.remove("error");
            return;
        }
        el.notice.textContent = text;
        el.notice.classList.toggle("error", !!isError);
        el.notice.hidden = false;
    }

    /* Fetch JSON, turning any non-2xx into a rejected promise that still
       carries the status: 503 and 401 need different words on screen. */
    function fetchJSON(url) {
        return fetch(url, { headers: { Accept: "application/json" } }).then(
            function (resp) {
                if (!resp.ok) {
                    var err = new Error("HTTP " + resp.status);
                    err.status = resp.status;
                    throw err;
                }
                return resp.json();
            }
        );
    }

    // ----------------------------------------------------------------- detail

    function setPanesVisible(visible) {
        el.panes.hidden = !visible;
        el.detailEmpty.hidden = visible;
    }

    function resetDetail() {
        selectedId = null;
        promptText = "";
        responseText = "";
        el.prompt.textContent = "";
        el.response.textContent = "";
        el.dlPrompt.disabled = true;
        el.dlResponse.disabled = true;
        setPanesVisible(false);
    }

    function markActive() {
        var items = el.list.querySelectorAll("[data-request-id]");
        for (var i = 0; i < items.length; i++) {
            items[i].classList.toggle(
                "active",
                items[i].getAttribute("data-request-id") === selectedId
            );
        }
    }

    function setPre(node, button, text) {
        if (!text) {
            node.textContent = I18N.raw_none;
            button.disabled = true;
            return;
        }
        node.textContent = text;
        button.disabled = false;
    }

    function loadDetail(requestId) {
        selectedId = requestId;
        promptText = "";
        responseText = "";
        el.prompt.textContent = I18N.raw_loading;
        el.response.textContent = I18N.raw_loading;
        el.dlPrompt.disabled = true;
        el.dlResponse.disabled = true;
        setPanesVisible(true);
        markActive();

        var base = "/v1/audit/raw/" + encodeURIComponent(requestId);
        /* Both halves settle independently: a missing response (a request that
           is still streaming, or one that errored upstream) must not blank the
           prompt that did store. */
        var req = fetchJSON(base + "?direction=req").catch(function () {
            return null;
        });
        var resp = fetchJSON(base + "?direction=resp").catch(function () {
            return null;
        });

        Promise.all([req, resp]).then(function (results) {
            /* A second click while these were in flight wins: drop a stale
               response rather than painting it over the newer selection. */
            if (selectedId !== requestId) {
                return;
            }
            var reqData = results[0];
            var respData = results[1];
            promptText = reqData && reqData.text ? reqData.text : "";
            responseText = respData && respData.text ? respData.text : "";
            setPre(el.prompt, el.dlPrompt, promptText);
            setPre(el.response, el.dlResponse, responseText);
            if (!reqData && !respData) {
                showNotice(I18N.raw_unavailable, true);
            } else {
                showNotice(null);
            }
        });
    }

    // ------------------------------------------------------------------- list

    function buildRow(event) {
        var li = document.createElement("li");
        li.setAttribute("data-request-id", event.request_id);
        li.className = "raw-row";
        [
            fmtTime(event.ts),
            String(event.request_id || "").slice(0, 12),
            fmtValue(event.model),
            fmtValue(event.token_id)
        ].forEach(function (text) {
            var cell = document.createElement("span");
            cell.textContent = text;
            li.appendChild(cell);
        });
        li.addEventListener("click", function () {
            loadDetail(event.request_id);
        });
        return li;
    }

    function renderList(events) {
        clear(el.list);
        el.empty.hidden = events.length > 0;
        events.forEach(function (event) {
            if (!event || !event.request_id) {
                return;
            }
            el.list.appendChild(buildRow(event));
        });
        /* Refresh keeps the current selection highlighted; a detail whose row
           has aged out of the window is cleared so the two panes cannot
           disagree. */
        if (selectedId) {
            var stillListed = el.list.querySelector(
                '[data-request-id="' + selectedId.replace(/"/g, '\\"') + '"]'
            );
            if (stillListed) {
                markActive();
            } else {
                resetDetail();
            }
        }
    }

    // ------------------------------------------------------------------- load

    function load() {
        if (inFlight) {
            return;
        }
        inFlight = true;
        el.status.textContent = I18N.raw_loading;

        var windowSeconds = parseInt(el.window.value, 10);
        var since = Date.now() / 1000 - windowSeconds;
        var url =
            "/v1/audit/raw?since=" +
            encodeURIComponent(since) +
            "&limit=" +
            PAGE_SIZE;

        fetchJSON(url).then(
            function (payload) {
                var events = payload.events || [];
                renderList(events);
                if (payload.enabled === false) {
                    showNotice(I18N.raw_disabled, false);
                    el.empty.hidden = true;
                } else {
                    showNotice(null);
                }
                el.status.textContent = "";
                inFlight = false;
            },
            function () {
                clear(el.list);
                el.empty.hidden = true;
                resetDetail();
                showNotice(I18N.raw_unavailable, true);
                el.status.textContent = "";
                inFlight = false;
            }
        );
    }

    // --------------------------------------------------------------- download

    function download(text, direction) {
        if (!text) {
            return;
        }
        var blob = new Blob([text], { type: "text/plain;charset=utf-8" });
        var objectUrl = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = objectUrl;
        a.download =
            "audit-" + (selectedId || "raw") + "-" + direction + ".txt";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(objectUrl);
    }

    // ----------------------------------------------------------------- events

    el.refresh.addEventListener("click", load);
    el.window.addEventListener("change", load);
    el.dlPrompt.addEventListener("click", function () {
        download(promptText, "req");
    });
    el.dlResponse.addEventListener("click", function () {
        download(responseText, "resp");
    });

    resetDetail();
    load();
    timer = setInterval(load, AUTO_REFRESH_MS);
})();