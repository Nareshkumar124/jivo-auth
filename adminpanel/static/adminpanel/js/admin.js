"use strict";
// Jivo Auth admin: small progressive enhancements. Everything works without it.
{
    const ready = (callback) => {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", callback);
        } else {
            callback();
        }
    };

    // Mobile navigation drawer.
    function initDrawer() {
        const button = document.getElementById("jv-menu-button");
        const backdrop = document.getElementById("jv-nav-backdrop");
        const nav = document.getElementById("nav-sidebar");

        if (!button || !nav) {
            return;
        }

        const setOpen = (open) => {
            document.body.classList.toggle("jv-nav-open", open);
            button.setAttribute("aria-expanded", String(open));
            if (backdrop) {
                backdrop.hidden = !open;
            }
            if (open) {
                const first = nav.querySelector("input, a");
                if (first) {
                    first.focus();
                }
            }
        };

        button.addEventListener("click", () => {
            setOpen(!document.body.classList.contains("jv-nav-open"));
        });

        if (backdrop) {
            backdrop.addEventListener("click", () => setOpen(false));
        }

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && document.body.classList.contains("jv-nav-open")) {
                setOpen(false);
                button.focus();
            }
        });
    }

    // Filter the sidebar as you type (Django's own filter expects its old markup).
    function initNavFilter() {
        const input = document.getElementById("nav-filter");
        const nav = document.getElementById("nav-sidebar");

        if (!input || !nav) {
            return;
        }

        const items = Array.from(nav.querySelectorAll(".jv-nav__item"));
        const sections = Array.from(nav.querySelectorAll(".jv-nav__section"));
        const noMatch = nav.querySelector(".jv-nav__no-match");

        const apply = () => {
            const query = input.value.trim().toLowerCase();
            let matches = 0;

            for (const item of items) {
                const show = !query || item.textContent.toLowerCase().includes(query);
                item.parentElement.hidden = !show;
                matches += show ? 1 : 0;
            }

            for (const section of sections) {
                section.hidden = !section.querySelector("li:not([hidden])");
            }

            if (noMatch) {
                noMatch.hidden = matches > 0;
            }
        };

        input.addEventListener("input", apply);

        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                const first = items.find((item) => !item.parentElement.hidden);
                if (first) {
                    event.preventDefault();
                    window.location.href = first.href;
                }
            }
        });

        apply();
    }

    // Messages can be dismissed.
    function initMessages() {
        document.querySelectorAll("ul.messagelist li").forEach((message) => {
            const close = document.createElement("button");
            close.type = "button";
            close.className = "jv-dismiss";
            close.setAttribute("aria-label", "Dismiss");
            close.textContent = "×";
            close.addEventListener("click", () => message.remove());
            message.appendChild(close);
        });
    }

    // Buttons with data-confirm ask first.
    function initConfirmations() {
        document.addEventListener("click", (event) => {
            const button = event.target.closest("[data-confirm]");

            if (button && !window.confirm(button.dataset.confirm)) {
                event.preventDefault();
                event.stopPropagation();
            }
        }, true);
    }

    // Submitting shows progress and prevents double submits.
    function initBusyForms() {
        document.addEventListener("submit", (event) => {
            const form = event.target;

            // Leave the changelist action form alone: "Go" may just render a
            // confirmation page, and the browser back button would find it busy.
            if (event.defaultPrevented || form.id === "changelist-form") {
                return;
            }

            const submitter = event.submitter;

            if (submitter && (submitter.tagName === "BUTTON" || submitter.type === "submit")) {
                // Let the browser send the submitter's name/value first.
                window.setTimeout(() => {
                    submitter.classList.add("is-busy");
                    submitter.setAttribute("aria-busy", "true");
                }, 0);
            }
        });

        // Coming back through the history cache must not leave buttons busy.
        window.addEventListener("pageshow", () => {
            document.querySelectorAll(".is-busy").forEach((element) => {
                element.classList.remove("is-busy");
                element.removeAttribute("aria-busy");
            });
        });
    }

    // Copy buttons: <button data-copy="#selector">.
    function initCopyButtons() {
        document.querySelectorAll("[data-copy]").forEach((button) => {
            button.addEventListener("click", async () => {
                const target = document.querySelector(button.dataset.copy);
                const label = button.querySelector("span");

                if (!target) {
                    return;
                }

                try {
                    await navigator.clipboard.writeText(target.textContent.trim());
                    if (label) {
                        label.textContent = "Copied";
                        window.setTimeout(() => { label.textContent = "Copy"; }, 2000);
                    }
                } catch (error) {
                    // Clipboard blocked (e.g. no HTTPS): select the text instead.
                    const range = document.createRange();
                    range.selectNodeContents(target);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                }
            });
        });
    }

    // <select data-autosubmit> submits its form on change (the docs'
    // application picker; a <noscript> button covers no JavaScript).
    function initAutoSubmit() {
        document.querySelectorAll("select[data-autosubmit]").forEach((select) => {
            select.addEventListener("change", () => select.form.submit());
        });
    }

    // Docs: the table of contents folds away on small screens (it sits
    // above the article there), and marks the section being read.
    function initDocsToc() {
        const toc = document.querySelector("[data-docs-toc]");
        const narrow = window.matchMedia("(max-width: 1024px)");

        if (toc) {
            toc.open = !narrow.matches;
            // Wide screens: always open, the summary is just a title.
            toc.querySelector("summary").addEventListener("click", (event) => {
                if (!narrow.matches) {
                    event.preventDefault();
                }
            });
            toc.addEventListener("click", (event) => {
                if (narrow.matches && event.target.closest("a")) {
                    toc.open = false;
                }
            });
            narrow.addEventListener("change", () => { toc.open = !narrow.matches; });
        }

        const links = [...document.querySelectorAll(".jv-docs__toc a[href^='#']")];
        const sections = links
            .map((link) => document.getElementById(link.hash.slice(1)))
            .filter(Boolean);

        if (!sections.length || !("IntersectionObserver" in window)) {
            return;
        }

        const visible = new Set();

        const mark = () => {
            const current = sections.find((section) => visible.has(section)) || null;
            links.forEach((link) => {
                if (current && link.hash === `#${current.id}`) {
                    link.setAttribute("aria-current", "true");
                } else {
                    link.removeAttribute("aria-current");
                }
            });
        };

        const observer = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    visible.add(entry.target);
                } else {
                    visible.delete(entry.target);
                }
            });
            mark();
        }, { rootMargin: "-80px 0px -55% 0px" });

        sections.forEach((section) => observer.observe(section));
    }

    // On small screens the filters sit above the list: fold the groups that
    // filter nothing, unless the user already opened or closed some (Django
    // keeps that choice in sessionStorage).
    function initCompactFilters() {
        const filters = document.getElementById("changelist-filter");

        if (!filters || !window.matchMedia("(max-width: 1024px)").matches) {
            return;
        }

        if (sessionStorage.getItem("django.admin.filtersState")) {
            return;
        }

        filters.querySelectorAll("details").forEach((group) => {
            const first = group.querySelector("ul li");

            if (first && first.classList.contains("selected")) {
                group.open = false;
            }
        });
    }

    const SVG_NS = "http://www.w3.org/2000/svg";

    function icon(name, size) {
        const svg = document.createElementNS(SVG_NS, "svg");
        svg.setAttribute("class", "jv-icon");
        svg.setAttribute("width", size);
        svg.setAttribute("height", size);
        svg.setAttribute("aria-hidden", "true");

        const use = document.createElementNS(SVG_NS, "use");
        use.setAttribute("href", `#jv-${name}`);
        svg.append(use);

        return svg;
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);

        if (className) {
            node.className = className;
        }
        if (text !== undefined) {
            node.textContent = text;
        }

        return node;
    }

    // "Users with access" (adminpanel/widgets.py). The markup already works:
    // checkboxes remove, the emails box adds. This adds search-as-you-type
    // through Django's autocomplete endpoint, Remove/Undo buttons, a filter,
    // and a summary of unsaved changes, all driving the same checkboxes.
    function initUserAccess() {
        document.querySelectorAll("[data-jv-access]").forEach(setupUserAccess);
    }

    function setupUserAccess(root) {
        const FILTER_FROM = 8;

        const name = root.dataset.name;
        const showVerification = root.dataset.showVerification === "true";
        const list = root.querySelector("[data-list]");
        const count = root.querySelector("[data-count]");
        const noun = root.querySelector("[data-count-noun]");
        const pending = root.querySelector("[data-pending]");
        const pendingText = root.querySelector("[data-pending-text]");
        const undoAll = root.querySelector("[data-undo-all]");
        const empty = root.querySelector("[data-empty]");
        const filterRow = root.querySelector("[data-filter-row]");
        const filter = root.querySelector("[data-filter]");
        const shown = root.querySelector("[data-shown]");
        const noMatch = root.querySelector("[data-nomatch]");
        const noMatchTerm = root.querySelector("[data-nomatch-term]");
        const emails = root.querySelector("textarea");
        const search = root.querySelector("[data-search]");
        const results = root.querySelector("[data-results]");

        root.classList.add("jv-access--enhanced");

        const rows = () => Array.from(list.querySelectorAll(".jv-access__row"));
        const rowFor = (id) => rows().find((row) => row.dataset.id === String(id));
        const nameOf = (row) => row.querySelector(".jv-access__name").textContent.trim();

        let dirty = false;

        function updateToggle(row) {
            const button = row.querySelector("[data-toggle]");

            if (!button) {
                return;
            }

            const removed = row.dataset.state === "removed";

            button.hidden = false;
            button.classList.toggle("is-undo", removed);
            button.replaceChildren(icon(removed ? "refresh" : "x", 14), document.createTextNode(removed ? "Undo" : "Remove"));
            button.setAttribute("aria-label", removed ? `Keep ${nameOf(row)}` : `Remove ${nameOf(row)}`);
        }

        function setState(row, state) {
            row.dataset.state = state;
            row.classList.remove("is-member", "is-added", "is-removed");
            row.classList.add(`is-${state}`);

            const check = row.querySelector(".jv-access__check");
            if (check) {
                check.checked = state !== "removed";
            }

            updateToggle(row);
        }

        function applyFilter() {
            const term = filter.value.trim().toLowerCase();
            const all = rows();
            let visible = 0;

            all.forEach((row) => {
                const match = !term || row.dataset.terms.includes(term);
                row.hidden = !match;
                visible += match ? 1 : 0;
            });

            shown.textContent = term ? `${visible} of ${all.length}` : "";
            noMatch.hidden = !term || visible > 0;
            noMatchTerm.textContent = filter.value.trim();
        }

        function refresh() {
            const all = rows();
            const added = all.filter((row) => row.dataset.state === "added").length;
            const removed = all.filter((row) => row.dataset.state === "removed").length;
            const members = all.length - removed;
            const changes = [];

            if (added) {
                changes.push(`${added} to add`);
            }
            if (removed) {
                changes.push(`${removed} to remove`);
            }

            count.textContent = members;
            noun.textContent = members === 1 ? "person has" : "people have";
            pendingText.textContent = changes.length ? `${changes.join(", ")}, not saved yet` : "";
            pending.hidden = !changes.length;
            empty.hidden = all.length > 0;
            filterRow.hidden = all.length < FILTER_FROM && !filter.value;
            dirty = changes.length > 0;

            applyFilter();
        }

        function flash(row) {
            row.classList.remove("is-flash");
            // Restart the animation.
            void row.offsetWidth;
            row.classList.add("is-flash");
            row.scrollIntoView({ block: "nearest" });
        }

        function tagsFor(user, note) {
            const tags = element("span", "jv-access__tags");

            if (user.employee_code) {
                const code = element("code", "jv-access__code", user.employee_code);
                code.title = "Employee code";
                tags.append(code);
            }

            if (user.is_active === false) {
                tags.append(element("span", "jv-badge jv-badge--neutral", "Inactive"));
            } else if (showVerification && user.is_verified === false) {
                tags.append(element("span", "jv-badge jv-badge--warning", "Email not verified"));
            }

            if (note) {
                tags.append(element("span", "jv-access__result-note", note));
            }

            return tags;
        }

        function identityFor(user) {
            const email = user.email || user.text;
            const identity = element("span", "jv-access__identity");

            identity.append(element("span", "jv-access__name", user.name || email));

            if (user.name) {
                identity.append(element("span", "jv-access__email", email));
            }

            return identity;
        }

        function avatarFor(user) {
            const words = (user.name || user.email || user.text || "?").trim().split(/\s+/);
            const avatar = element("span", "jv-avatar", words.slice(0, 2).map((word) => word[0]).join("").toUpperCase());
            avatar.setAttribute("aria-hidden", "true");

            return avatar;
        }

        function buildRow(user) {
            const row = element("li", "jv-access__row");
            row.dataset.id = String(user.id);
            row.dataset.terms = [user.name, user.email || user.text, user.employee_code].filter(Boolean).join(" ").toLowerCase();

            if (user.is_active === false) {
                row.classList.add("is-inactive");
            }

            const person = element("label", "jv-access__person");
            const check = element("input", "jv-access__check");
            check.type = "checkbox";
            check.name = name;
            check.value = String(user.id);
            check.checked = true;
            person.append(check, avatarFor(user), identityFor(user));

            const tags = tagsFor(user);
            tags.append(
                element("span", "jv-badge jv-badge--good jv-access__flag jv-access__flag--added", "Will be added"),
                element("span", "jv-badge jv-badge--critical jv-access__flag jv-access__flag--removed", "Will be removed"),
            );

            const toggle = element("button", "jv-access__toggle");
            toggle.type = "button";
            toggle.dataset.toggle = "";

            row.append(person, tags, toggle);

            return row;
        }

        list.addEventListener("click", (event) => {
            const button = event.target.closest("[data-toggle]");

            if (!button) {
                return;
            }

            const row = button.closest(".jv-access__row");

            if (row.dataset.state === "added") {
                // Never saved: just drop it.
                row.remove();
                (search || filter).focus();
            } else {
                setState(row, row.dataset.state === "removed" ? "member" : "removed");
            }

            refresh();
        });

        undoAll.addEventListener("click", () => {
            rows().forEach((row) => {
                if (row.dataset.state === "added") {
                    row.remove();
                } else if (row.dataset.state === "removed") {
                    setState(row, "member");
                }
            });

            refresh();
        });

        filter.addEventListener("input", applyFilter);
        filter.addEventListener("keydown", (event) => {
            // Enter in a search field would submit the whole form.
            if (event.key === "Enter") {
                event.preventDefault();
            }
            if (event.key === "Escape" && filter.value) {
                event.preventDefault();
                filter.value = "";
                applyFilter();
            }
        });

        rows().forEach(updateToggle);
        refresh();

        // Leaving with unsaved changes asks first.
        const form = root.closest("form");
        let submitting = false;

        if (form) {
            form.addEventListener("submit", () => {
                submitting = true;
            });
        }

        window.addEventListener("beforeunload", (event) => {
            if (!submitting && (dirty || (emails && emails.value.trim()))) {
                event.preventDefault();
                event.returnValue = "";
            }
        });

        if (!search || !results || !root.dataset.searchUrl) {
            return;
        }

        root.querySelector("[data-picker]").hidden = false;

        let found = [];
        let active = -1;
        let timer = null;
        let controller = null;

        const options = () => Array.from(results.querySelectorAll('[role="option"]'));

        function open() {
            results.hidden = false;
            search.setAttribute("aria-expanded", "true");
        }

        function close() {
            results.hidden = true;
            active = -1;
            search.setAttribute("aria-expanded", "false");
            search.removeAttribute("aria-activedescendant");
        }

        function status(text) {
            const item = element("li", "jv-access__result-status", text);
            item.setAttribute("role", "presentation");

            return item;
        }

        function render(users, more, term) {
            found = users;
            active = -1;
            search.removeAttribute("aria-activedescendant");
            results.replaceChildren();

            if (!users.length) {
                results.append(status(`No accounts match “${term}”.`));
                return;
            }

            users.forEach((user, index) => {
                const row = rowFor(user.id);
                const state = row ? row.dataset.state : "";
                const listed = state === "member" || state === "added";
                const option = element("li", "jv-access__result");

                option.id = `${search.id}_option_${index}`;
                option.dataset.index = String(index);
                option.setAttribute("role", "option");
                option.setAttribute("aria-selected", "false");

                if (listed) {
                    option.setAttribute("aria-disabled", "true");
                }

                option.append(
                    avatarFor(user),
                    identityFor(user),
                    tagsFor(user, listed ? "Has access" : state === "removed" ? "Select to keep" : ""),
                );
                results.append(option);
            });

            if (more) {
                results.append(status("More accounts match: keep typing to narrow the list."));
            }
        }

        async function lookup(term) {
            if (controller) {
                controller.abort();
            }
            controller = new AbortController();

            const url = new URL(root.dataset.searchUrl, window.location.href);
            url.searchParams.set("term", term);

            results.replaceChildren(status("Searching…"));
            open();

            try {
                const response = await fetch(url, {
                    credentials: "same-origin",
                    headers: { Accept: "application/json" },
                    signal: controller.signal,
                });

                if (response.status === 403) {
                    results.replaceChildren(status("You don't have permission to search users."));
                    return;
                }
                if (!response.ok) {
                    throw new Error(String(response.status));
                }

                const data = await response.json();

                if (search.value.trim() === term) {
                    render(data.results || [], Boolean(data.pagination && data.pagination.more), term);
                }
            } catch (error) {
                if (error.name !== "AbortError") {
                    results.replaceChildren(status("Search failed. Check your connection and try again."));
                }
            }
        }

        function highlight(index) {
            const all = options();

            if (!all.length) {
                return;
            }

            active = (index + all.length) % all.length;

            all.forEach((option, position) => {
                const on = position === active;
                option.classList.toggle("is-active", on);
                option.setAttribute("aria-selected", String(on));
            });

            search.setAttribute("aria-activedescendant", all[active].id);
            all[active].scrollIntoView({ block: "nearest" });
        }

        function choose(option) {
            const user = found[Number(option.dataset.index)];

            if (!user) {
                return;
            }

            let row = rowFor(user.id);

            if (!row) {
                row = buildRow(user);
                list.prepend(row);
                setState(row, "added");
            } else if (row.dataset.state === "removed") {
                setState(row, "member");
            }

            // Show the row even if the filter would hide it.
            filter.value = "";
            refresh();
            flash(row);

            search.value = "";
            close();
            search.focus();
        }

        search.addEventListener("input", () => {
            const term = search.value.trim();

            clearTimeout(timer);

            if (!term) {
                if (controller) {
                    controller.abort();
                }
                close();
                return;
            }

            timer = setTimeout(() => lookup(term), 200);
        });

        search.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                // Never submit the form from here.
                event.preventDefault();

                if (!results.hidden && active >= 0) {
                    choose(options()[active]);
                }
            } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();

                if (results.hidden) {
                    if (search.value.trim()) {
                        lookup(search.value.trim());
                    }
                    return;
                }

                highlight(active + (event.key === "ArrowDown" ? 1 : -1));
            } else if (event.key === "Escape" && !results.hidden) {
                event.preventDefault();
                close();
            }
        });

        search.addEventListener("focus", () => {
            if (search.value.trim() && results.childElementCount) {
                open();
            }
        });

        search.addEventListener("blur", close);

        // Keep focus in the search box while clicking a result.
        results.addEventListener("mousedown", (event) => event.preventDefault());

        results.addEventListener("click", (event) => {
            const option = event.target.closest('[role="option"]');

            if (option) {
                choose(option);
            }
        });
    }

    ready(() => {
        initCompactFilters();
        initDrawer();
        initNavFilter();
        initMessages();
        initConfirmations();
        initBusyForms();
        initCopyButtons();
        initAutoSubmit();
        initDocsToc();
        initUserAccess();
    });
}
