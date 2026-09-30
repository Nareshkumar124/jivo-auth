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
    });
}
