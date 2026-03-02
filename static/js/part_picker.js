/* global window, document */
(function () {
    const DEFAULT_PLACEHOLDER = "\u0627\u0628\u062d\u062b \u0628\u0631\u0642\u0645 \u0627\u0644\u0642\u0637\u0639\u0629 \u0623\u0648 \u0627\u0633\u0645 \u0627\u0644\u0635\u0646\u0641";
    const DEFAULT_HELP = "\u0627\u0643\u062a\u0628 \u0631\u0642\u0645 \u0627\u0644\u0642\u0637\u0639\u0629 \u0623\u0648 \u0627\u0633\u0645 \u0627\u0644\u0635\u0646\u0641 \u062b\u0645 \u0627\u062e\u062a\u0631 \u0645\u0646 \u0627\u0644\u0642\u0627\u0626\u0645\u0629.";
    const NO_RESULTS_TEXT = "\u0644\u0627 \u062a\u0648\u062c\u062f \u0646\u062a\u0627\u0626\u062c \u0645\u0637\u0627\u0628\u0642\u0629.";
    const MAX_RESULTS = 20;

    function normalize(value) {
        return (value || "").trim().toLowerCase();
    }

    function buildPartItems(selectEl) {
        const items = [];
        selectEl.querySelectorAll("option").forEach((optionEl) => {
            const id = (optionEl.value || "").trim();
            const label = (optionEl.textContent || "").trim();
            if (!id || !label) return;
            const partNumber = (label.split(" - ")[0] || "").trim();
            items.push({
                id,
                label,
                labelNorm: normalize(label),
                partNumber,
                partNumberNorm: normalize(partNumber),
            });
        });
        return items;
    }

    function resolveTypedValue(items, typed) {
        const typedNorm = normalize(typed);
        if (!typedNorm) return null;

        const exact = items.find((item) => item.labelNorm === typedNorm || item.partNumberNorm === typedNorm);
        if (exact) return exact;

        const starts = items.filter(
            (item) => item.partNumberNorm.startsWith(typedNorm) || item.labelNorm.startsWith(typedNorm)
        );
        if (starts.length === 1) return starts[0];

        return null;
    }

    function filterItems(items, typed) {
        const token = normalize(typed);
        if (!token) return items.slice(0, MAX_RESULTS);
        return items
            .filter((item) => item.partNumberNorm.includes(token) || item.labelNorm.includes(token))
            .slice(0, MAX_RESULTS);
    }

    function renderMenu(menuEl, filtered, activeIndex) {
        menuEl.innerHTML = "";

        if (!filtered.length) {
            const emptyEl = document.createElement("div");
            emptyEl.className = "delta-part-picker-empty";
            emptyEl.textContent = NO_RESULTS_TEXT;
            menuEl.appendChild(emptyEl);
            return;
        }

        filtered.forEach((item, index) => {
            const optionEl = document.createElement("button");
            optionEl.type = "button";
            optionEl.className = "delta-part-picker-option";
            if (index === activeIndex) optionEl.classList.add("active");
            optionEl.dataset.partId = item.id;
            optionEl.innerHTML = `<span class="delta-part-picker-title">${item.label}</span>`;
            menuEl.appendChild(optionEl);
        });
    }

    function initOne(selectEl) {
        if (!selectEl || selectEl.dataset.partPickerReady === "1") return;
        const form = selectEl.closest("form");
        if (!form) return;

        const originalName = (selectEl.getAttribute("name") || "").trim();
        if (!originalName) return;

        const items = buildPartItems(selectEl);
        const selectedOption = selectEl.options[selectEl.selectedIndex];
        const selectedId = selectedOption && selectedOption.value ? String(selectedOption.value) : "";
        const selectedLabel = selectedOption && selectedOption.value ? selectedOption.textContent.trim() : "";
        const required = selectEl.required;

        const wrapperEl = document.createElement("div");
        wrapperEl.className = "delta-part-picker";

        const inputEl = document.createElement("input");
        inputEl.type = "text";
        inputEl.className = "form-control part-picker-input";
        inputEl.placeholder = selectEl.getAttribute("data-part-placeholder") || DEFAULT_PLACEHOLDER;
        inputEl.value = selectedLabel;
        inputEl.autocomplete = "off";

        const hiddenEl = document.createElement("input");
        hiddenEl.type = "hidden";
        hiddenEl.name = originalName;
        hiddenEl.value = selectedId;
        hiddenEl.className = "part-picker-id";

        const menuEl = document.createElement("div");
        menuEl.className = "delta-part-picker-menu";

        const helpEl = document.createElement("div");
        helpEl.className = "form-text part-picker-help";
        helpEl.textContent = selectEl.getAttribute("data-part-help") || DEFAULT_HELP;

        let isOpen = false;
        let filteredItems = [];
        let activeIndex = -1;
        let blurTimer = null;

        function openMenu() {
            if (!isOpen) {
                isOpen = true;
                menuEl.classList.add("show");
            }
        }

        function closeMenu() {
            isOpen = false;
            activeIndex = -1;
            menuEl.classList.remove("show");
        }

        function setInvalidState() {
            const hasTyped = normalize(inputEl.value).length > 0;
            inputEl.classList.toggle("is-invalid", required && !hiddenEl.value && hasTyped);
        }

        function selectItem(item) {
            if (!item) return;
            hiddenEl.value = item.id;
            inputEl.value = item.label;
            inputEl.classList.remove("is-invalid");
            closeMenu();
        }

        function updateMenu() {
            filteredItems = filterItems(items, inputEl.value);
            activeIndex = filteredItems.length ? 0 : -1;
            renderMenu(menuEl, filteredItems, activeIndex);
            openMenu();
        }

        function syncHiddenFromTyped() {
            const match = resolveTypedValue(items, inputEl.value);
            hiddenEl.value = match ? match.id : "";
            setInvalidState();
        }

        inputEl.addEventListener("input", () => {
            hiddenEl.value = "";
            setInvalidState();
            updateMenu();
        });

        inputEl.addEventListener("focus", () => {
            if (blurTimer) {
                window.clearTimeout(blurTimer);
                blurTimer = null;
            }
            updateMenu();
        });

        inputEl.addEventListener("keydown", (event) => {
            if (!isOpen && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
                updateMenu();
            }

            if (!isOpen) {
                if (event.key === "Enter") {
                    const uniqueMatch = resolveTypedValue(items, inputEl.value);
                    if (uniqueMatch) {
                        selectItem(uniqueMatch);
                    }
                }
                return;
            }

            if (event.key === "ArrowDown") {
                event.preventDefault();
                if (filteredItems.length) {
                    activeIndex = (activeIndex + 1) % filteredItems.length;
                    renderMenu(menuEl, filteredItems, activeIndex);
                }
                return;
            }

            if (event.key === "ArrowUp") {
                event.preventDefault();
                if (filteredItems.length) {
                    activeIndex = activeIndex <= 0 ? filteredItems.length - 1 : activeIndex - 1;
                    renderMenu(menuEl, filteredItems, activeIndex);
                }
                return;
            }

            if (event.key === "Enter") {
                event.preventDefault();
                if (activeIndex >= 0 && filteredItems[activeIndex]) {
                    selectItem(filteredItems[activeIndex]);
                } else {
                    syncHiddenFromTyped();
                    closeMenu();
                }
                return;
            }

            if (event.key === "Escape") {
                event.preventDefault();
                closeMenu();
            }
        });

        inputEl.addEventListener("blur", () => {
            blurTimer = window.setTimeout(() => {
                syncHiddenFromTyped();
                closeMenu();
            }, 130);
        });

        menuEl.addEventListener("mousedown", (event) => {
            event.preventDefault();
            const optionEl = event.target.closest(".delta-part-picker-option");
            if (!optionEl) return;
            const selected = items.find((item) => item.id === optionEl.dataset.partId);
            selectItem(selected);
        });

        document.addEventListener("click", (event) => {
            if (!wrapperEl.contains(event.target)) {
                closeMenu();
            }
        });

        form.addEventListener("submit", (event) => {
            syncHiddenFromTyped();
            if (required && !hiddenEl.value) {
                event.preventDefault();
                inputEl.classList.add("is-invalid");
                inputEl.focus();
            }
        });

        selectEl.dataset.partPickerReady = "1";
        selectEl.removeAttribute("name");
        selectEl.required = false;
        selectEl.disabled = true;
        selectEl.classList.add("d-none");

        wrapperEl.appendChild(inputEl);
        wrapperEl.appendChild(menuEl);
        wrapperEl.appendChild(hiddenEl);
        wrapperEl.appendChild(helpEl);
        selectEl.insertAdjacentElement("afterend", wrapperEl);
    }

    function initAll(root) {
        const scope = root || document;
        const selects = scope.querySelectorAll("select.js-part-select");
        selects.forEach((selectEl) => initOne(selectEl));
    }

    window.deltaInitPartPickers = initAll;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => initAll(document));
    } else {
        initAll(document);
    }
})();
