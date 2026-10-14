"use strict";
// Jivo Support: small progressive enhancements. Every page works without it.
{
    const ready = (callback) => {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", callback);
        } else {
            callback();
        }
    };

    const SVG_NS = "http://www.w3.org/2000/svg";

    function icon(name, size) {
        const svg = document.createElementNS(SVG_NS, "svg");
        svg.setAttribute("class", "icon");
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

    function storage(action, key, value) {
        try {
            if (action === "get") {
                return window.localStorage.getItem(key);
            }
            window.localStorage.setItem(key, value);
        } catch (error) {
            // Private windows and blocked storage: the default theme applies.
        }
        return null;
    }

    // Theme: auto (OS), light, dark; the choice is remembered per browser.
    function initTheme() {
        const button = document.querySelector("[data-theme-toggle]");
        const order = ["auto", "light", "dark"];
        const labels = { auto: "Theme: automatic", light: "Theme: light", dark: "Theme: dark" };

        const apply = (theme) => {
            if (theme === "auto") {
                document.documentElement.removeAttribute("data-theme");
            } else {
                document.documentElement.setAttribute("data-theme", theme);
            }
            if (button) {
                button.setAttribute("aria-label", labels[theme]);
                button.title = labels[theme];
                button.querySelector("use").setAttribute("href", theme === "dark" ? "#jv-moon" : theme === "light" ? "#jv-sun" : "#jv-contrast");
            }
        };

        let theme = storage("get", "jivo-support-theme") || "auto";
        apply(order.includes(theme) ? theme : "auto");

        if (button) {
            button.addEventListener("click", () => {
                theme = order[(order.indexOf(theme) + 1) % order.length];
                storage("set", "jivo-support-theme", theme);
                apply(theme);
            });
        }
    }

    // Mobile navigation drawer.
    function initDrawer() {
        const button = document.querySelector("[data-menu-button]");
        const backdrop = document.querySelector("[data-nav-backdrop]");
        const nav = document.getElementById("sidenav");

        if (!button || !nav) {
            return;
        }

        const setOpen = (open) => {
            document.body.classList.toggle("nav-open", open);
            button.setAttribute("aria-expanded", String(open));
            if (open) {
                const first = nav.querySelector("a");
                if (first) {
                    first.focus();
                }
            }
        };

        button.addEventListener("click", () => setOpen(!document.body.classList.contains("nav-open")));
        if (backdrop) {
            backdrop.addEventListener("click", () => setOpen(false));
        }
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && document.body.classList.contains("nav-open")) {
                setOpen(false);
                button.focus();
            }
        });
    }

    function initMessages() {
        document.querySelectorAll("[data-message]").forEach((message) => {
            const close = element("button", "message__dismiss", "×");
            close.type = "button";
            close.setAttribute("aria-label", "Dismiss");
            close.addEventListener("click", () => message.remove());
            message.append(close);
        });
    }

    // <button data-confirm="…"> asks first.
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
            const submitter = event.submitter;
            if (event.defaultPrevented || !submitter || event.target.method.toLowerCase() === "get") {
                return;
            }
            window.setTimeout(() => {
                submitter.classList.add("is-busy");
                submitter.setAttribute("aria-busy", "true");
            }, 0);
        });

        window.addEventListener("pageshow", () => {
            document.querySelectorAll(".is-busy").forEach((node) => {
                node.classList.remove("is-busy");
                node.removeAttribute("aria-busy");
            });
        });
    }

    // <select data-autosubmit> submits its form on change.
    function initAutoSubmit() {
        document.querySelectorAll("select[data-autosubmit]").forEach((select) => {
            select.addEventListener("change", () => select.form.requestSubmit());
        });
    }

    // <select data-depends-on="id_department"> offers only the options whose
    // data-parent matches the chosen department.
    function initDependentSelects() {
        document.querySelectorAll("select[data-depends-on]").forEach((select) => {
            const parent = document.getElementById(select.dataset.dependsOn);
            if (!parent) {
                return;
            }

            const options = Array.from(select.options).slice(1);
            const placeholder = select.options[0];
            const emptyLabel = select.dataset.emptyLabel || placeholder.textContent;
            const defaultLabel = placeholder.textContent;

            const apply = () => {
                const chosen = parent.value;
                const current = select.value;

                select.replaceChildren(placeholder);
                placeholder.textContent = chosen ? defaultLabel : emptyLabel;
                options.forEach((option) => {
                    if (!chosen || option.dataset.parent === chosen) {
                        select.append(option);
                    }
                });

                select.disabled = !chosen;
                select.value = Array.from(select.options).some((option) => option.value === current) ? current : "";

                // A department with a single application: pick it.
                if (chosen && select.options.length === 2 && !select.value) {
                    select.value = select.options[1].value;
                }
            };

            parent.addEventListener("change", apply);
            apply();
        });
    }

    // The file input lists what was picked and flags files over the limit.
    function initUploads() {
        document.querySelectorAll("[data-upload]").forEach((root) => {
            const input = root.querySelector("input[type=file]");
            const list = root.querySelector("[data-upload-list]");
            const label = root.querySelector("[data-upload-label]");

            if (!input || !list) {
                return;
            }

            root.classList.add("upload--enhanced");
            const defaultLabel = label ? label.textContent : "";
            const maxBytes = Number(input.dataset.maxBytes || 0);

            const format = (bytes) => {
                if (bytes < 1024) {
                    return `${bytes} B`;
                }
                if (bytes < 1024 * 1024) {
                    return `${(bytes / 1024).toFixed(0)} KB`;
                }
                return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
            };

            const render = () => {
                const files = Array.from(input.files || []);
                list.replaceChildren();
                list.hidden = files.length === 0;

                if (label) {
                    label.textContent = files.length
                        ? `${files.length} file${files.length === 1 ? "" : "s"} selected. Click to change.`
                        : defaultLabel;
                }

                files.forEach((file) => {
                    const tooBig = maxBytes && file.size > maxBytes;
                    const item = element("li", tooBig ? "is-too-big" : "");
                    item.append(icon(tooBig ? "alert" : "file", 14), element("span", "", file.name), element("span", "muted", format(file.size)));
                    if (tooBig) {
                        item.append(element("span", "", `over ${input.dataset.maxLabel || "the limit"}`));
                    }
                    list.append(item);
                });
            };

            input.addEventListener("change", render);
            ["dragenter", "dragover"].forEach((name) => root.addEventListener(name, () => root.classList.add("is-dragover")));
            ["dragleave", "drop"].forEach((name) => root.addEventListener(name, () => root.classList.remove("is-dragover")));
            render();
        });
    }

    // The reply form has several submit buttons (reply, internal note,
    // request information); each sets the hidden action. Ctrl/Cmd+Enter replies.
    function initReplyActions() {
        document.querySelectorAll("[data-set-action]").forEach((button) => {
            button.addEventListener("click", () => {
                const field = button.form && button.form.querySelector("[data-reply-action]");
                if (field) {
                    field.value = button.dataset.setAction;
                }
            });
        });

        document.querySelectorAll(".reply textarea").forEach((textarea) => {
            textarea.addEventListener("keydown", (event) => {
                if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                    event.preventDefault();
                    const field = textarea.form.querySelector("[data-reply-action]");
                    if (field) {
                        field.value = "reply";
                    }
                    textarea.form.requestSubmit();
                }
            });
        });
    }

    // Ticket table: select all, the count, and the bulk bar's parameter field.
    function initBulk() {
        const form = document.querySelector("[data-bulk-form]");
        if (!form) {
            return;
        }

        const all = form.querySelector("[data-select-all]");
        const boxes = () => Array.from(document.querySelectorAll("input[form='bulk-form'][name='tickets']"));
        const count = form.querySelector("[data-bulk-count]");
        const submit = form.querySelector("[data-bulk-submit]");
        const action = form.querySelector("select[name='action']");

        const refresh = () => {
            const chosen = boxes().filter((box) => box.checked).length;
            if (count) {
                count.textContent = chosen ? `${chosen} selected` : "Select tickets to act on them";
            }
            if (submit) {
                submit.disabled = chosen === 0 || !action.value;
            }
            if (all) {
                all.checked = chosen > 0 && chosen === boxes().length;
                all.indeterminate = chosen > 0 && chosen < boxes().length;
            }
        };

        if (all) {
            all.addEventListener("change", () => {
                boxes().forEach((box) => { box.checked = all.checked; });
                refresh();
            });
        }

        document.addEventListener("change", (event) => {
            if (event.target.matches("input[form='bulk-form'][name='tickets']") || event.target === action) {
                refresh();
            }
        });

        refresh();
    }

    ready(() => {
        initTheme();
        initDrawer();
        initMessages();
        initConfirmations();
        initBusyForms();
        initAutoSubmit();
        initDependentSelects();
        initUploads();
        initReplyActions();
        initBulk();
    });
}
