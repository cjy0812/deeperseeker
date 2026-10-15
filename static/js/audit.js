/* Audit console.
 *
 * Plain DOM APIs: no framework, no build step, no client-side dictionary. The
 * page is a shell that fills itself from the two JSON audit endpoints, and
 * every user-visible string comes from the #i18n blob the server rendered.
 *
 * All dynamic values go in through textContent, never innerHTML: rows carry
 * database-sourced fields (token ids, reasons, timestamps) and none of them
 * may be parsed as markup.
 */
(function () {
    "use strict";

    var I18N = JSON.parse(document.getElementById("i18n").textContent);

    /* Re-read interval when "auto-refresh" is ticked. Slow enough that an
       operator can leave the tab open without hammering the audit DB. */
    var AUTO_REFRESH_MS = 15000;

    var el = {
        window: document.getElementById("window"),
        refresh: document.getElementById("refresh"),
        auto: document.getElementById("auto"),
        status: document.getElementById("status"),
        notice: document.getElementById("notice"),
        reasons: document.getElementById("reasons"),
        counters: document.getElementById("counters"),
        rollovers: document.getElementById("rollovers"),
        rlBody: document.getElementById("rl-body"),
        rlEmpty: document.getElementById("rl-empty")
    };

    var timer = null;
    var inFlight = false;

    // ------------------------------------------------------------------ utils

    function clear(node) {
        while (node.firstChild) {
            node.removeChild(node.firstChild);
        }
    }

    function setCard(metric, value) {
        var nodes = document.querySelectorAll('[data-metric="' + metric + '"]');
        for (var i = 0; i < nodes.length; i++) {
            nodes[i].textContent = value;
        }
    }

    function fmtCount(value) {
        return value === null || value === undefined ? I18N.none : String(value);
    }

    function fmtMs(value) {
        return value === null || value === undefined
            ? I18N.none
            : Math.round(value) + " ms";
    }

    function fmtPct(value) {
        return value === null || value === undefined
            ? I18N.none
            : (value * 100).toFixed(2) + "%";
    }

    function fmtTime(seconds) {
        if (!seconds) {
            return I18N.none;
        }
        var when = new Date(seconds * 1000);
        return isNaN(when.getTime()) ? I18N.none : when.toLocaleString();
    }

    /* Map a stored reason to its label. The DB stores free-form strings, so an
       unrecognized one is shown verbatim rather than swallowed. */
    function reasonLabel(reason) {
        if (!reason) {
            return I18N.reasons.unknown;
        }
        if (I18N.reasons[reason]) {
            return I18N.reasons[reason];
        }
        if (reason.indexOf("http_4") === 0 && reason !== "http_429") {
            return I18N.reasons.http_other;
        }
        return reason;
    }

    function kvRow(label, value) {
        var li = document.createElement("li");
        var key = document.createElement("span");
        key.className = "kv-key";
        key.textContent = label;
        var val = document.createElement("span");
        val.className = "kv-value";
        val.textContent = value;
        li.appendChild(key);
        li.appendChild(val);
        return li;
    }

    function emptyRow(node, text) {
        var li = document.createElement("li");
        li.className = "empty";
        li.textContent = text;
        node.appendChild(li);
    }

    function showNotice(text, isError) {
        if (!text) {
            el.notice.hidden = true;
            el.notice.textContent = "";
            return;
        }
        el.notice.textContent = text;
        el.notice.classList.toggle("error", !!isError);
        el.notice.hidden = false;
    }

    // ---------------------------------------------------------------- render

    function renderPerf(data) {
        var latency = data.latency_ms || {};
        setCard("requests", fmtCount(data.requests));
        setCard("error_rate", fmtPct(data.error_rate));
        setCard("p50", fmtMs(latency.p50));
        setCard("p95", fmtMs(latency.p95));
        setCard("p99", fmtMs(latency.p99));

        var reasons = data.rate_limit_reasons || {};
        var keys = Object.keys(reasons);
        clear(el.reasons);
        if (!keys.length) {
            emptyRow(el.reasons, I18N.empty);
        } else {
            keys.sort(function (a, b) {
                return reasons[b] - reasons[a];
            });
            keys.forEach(function (key) {
                el.reasons.appendChild(kvRow(reasonLabel(key), fmtCount(reasons[key])));
            });
        }

        /* Recovery counters are shown at a fixed set of rows (missing kinds
           read as 0) so the panel does not reshuffle between refreshes. */
        var counters = data.counters || {};
        clear(el.counters);
        ["rate_limit", "rate_recover", "failover", "retry"].forEach(function (kind) {
            el.counters.appendChild(
                kvRow(I18N.counters[kind], fmtCount(counters[kind] || 0))
            );
        });

        clear(el.rollovers);
        el.rollovers.appendChild(
            kvRow(I18N.counters.rollover, fmtCount(counters.rollover || 0))
        );
    }

    function renderRateLimits(payload) {
        var rows = payload.tokens || [];
        setCard("tokens_limited", String(rows.length));
        clear(el.rlBody);
        el.rlEmpty.hidden = rows.length > 0;
        rows.forEach(function (row) {
            var tr = document.createElement("tr");
            [
                fmtCount(row.token_id),
                fmtCount(row.limit_count),
                fmtCount(row.http_429),
                fmtCount(row.sse_rate_limit),
                fmtCount(row.http_other),
                fmtCount(row.recover_count),
                fmtTime(row.last_limited_at),
                reasonLabel(row.last_reason)
            ].forEach(function (text) {
                var td = document.createElement("td");
                td.textContent = text;
                tr.appendChild(td);
            });
            el.rlBody.appendChild(tr);
        });
    }

    // ------------------------------------------------------------------ load

    function fetchJSON(url) {
        return fetch(url, { headers: { Accept: "application/json" } }).then(
            function (resp) {
                if (!resp.ok) {
                    throw new Error("HTTP " + resp.status);
                }
                return resp.json();
            }
        );
    }

    function load() {
        if (inFlight) {
            return;
        }
        inFlight = true;
        el.status.textContent = I18N.loading;

        var windowSeconds = parseInt(el.window.value, 10);
        var since = Math.floor(Date.now() / 1000) - windowSeconds;
        /* Each endpoint settles on its own: one store failure must not blank
           the half of the page that did load. */
        var perf = fetchJSON("/v1/audit/perf?window=" + windowSeconds).catch(
            function () {
                return null;
            }
        );
        var limits = fetchJSON("/v1/audit/rate-limits?since=" + since).catch(
            function () {
                return null;
            }
        );

        Promise.all([perf, limits]).then(function (results) {
            var perfData = results[0];
            var limitData = results[1];
            if (perfData) {
                renderPerf(perfData);
            }
            if (limitData) {
                renderRateLimits(limitData);
            }
            if (!perfData || !limitData) {
                showNotice(I18N.unavailable, true);
            } else if (perfData.enabled === false) {
                showNotice(I18N.disabled, false);
            } else {
                showNotice(null);
            }
            el.status.textContent = "";
            inFlight = false;
        });
    }

    // ----------------------------------------------------------------- events

    el.refresh.addEventListener("click", load);
    el.window.addEventListener("change", load);
    el.auto.addEventListener("change", function () {
        if (timer) {
            clearInterval(timer);
            timer = null;
        }
        if (el.auto.checked) {
            timer = setInterval(load, AUTO_REFRESH_MS);
            load();
        }
    });

    load();
})();