(function () {
    if (window.__deltaForceArabicUiApplied) return;
    window.__deltaForceArabicUiApplied = true;

    const phraseEntries = [
        ["Order ID", "\u0631\u0642\u0645 \u0627\u0644\u0637\u0644\u0628"],
        ["Seller", "\u0627\u0644\u0628\u0627\u0626\u0639"],
        ["Customer", "\u0627\u0644\u0639\u0645\u064a\u0644"],
        ["Ledger Entries", "\u0642\u064a\u0648\u062f \u0627\u0644\u062f\u0641\u062a\u0631"],
        ["Payments", "\u0627\u0644\u062f\u0641\u0639\u0627\u062a"],
        ["Credit Notes", "\u0627\u0644\u0625\u0634\u0639\u0627\u0631\u0627\u062a \u0627\u0644\u062f\u0627\u0626\u0646\u0629"],
        ["Add Payment", "\u0625\u0636\u0627\u0641\u0629 \u062f\u0641\u0639\u0629"],
        ["Create Credit Note", "\u0625\u0646\u0634\u0627\u0621 \u0625\u0634\u0639\u0627\u0631 \u062f\u0627\u0626\u0646"],
        ["Ledger Statement", "\u0643\u0634\u0641 \u062d\u0633\u0627\u0628 \u0627\u0644\u0639\u0645\u064a\u0644"],

        ["Amount", "\u0627\u0644\u0645\u0628\u0644\u063a"],
        ["Method", "\u0627\u0644\u0637\u0631\u064a\u0642\u0629"],
        ["Reference", "\u0627\u0644\u0645\u0631\u062c\u0639"],
        ["Reason", "\u0627\u0644\u0633\u0628\u0628"],
        ["Date", "\u0627\u0644\u062a\u0627\u0631\u064a\u062e"],
        ["Type", "\u0627\u0644\u0646\u0648\u0639"],
        ["Status", "\u0627\u0644\u062d\u0627\u0644\u0629"],
        ["Notes", "\u0645\u0644\u0627\u062d\u0638\u0627\u062a"],
        ["By", "\u0628\u0648\u0627\u0633\u0637\u0629"],

        ["Customer Total", "\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0639\u0645\u064a\u0644"],
        ["Subtotal", "\u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0641\u0631\u0639\u064a"],

        ["Back to Search", "\u0627\u0644\u0639\u0648\u062f\u0629 \u0625\u0644\u0649 \u0627\u0644\u0628\u062d\u062b"],
        ["Back to Orders", "\u0627\u0644\u0639\u0648\u062f\u0629 \u0625\u0644\u0649 \u0627\u0644\u0637\u0644\u0628\u0627\u062a"],
        ["Back to Transfers", "\u0627\u0644\u0639\u0648\u062f\u0629 \u0625\u0644\u0649 \u0627\u0644\u062a\u062d\u0648\u064a\u0644\u0627\u062a"],

        ["No customer orders found.", "\u0644\u0627 \u062a\u0648\u062c\u062f \u0637\u0644\u0628\u0627\u062a \u0644\u0644\u0639\u0645\u064a\u0644."],
        ["No ledger entries.", "\u0644\u0627 \u062a\u0648\u062c\u062f \u0642\u064a\u0648\u062f \u0641\u064a \u0627\u0644\u062f\u0641\u062a\u0631."],
        ["No data available.", "\u0644\u0627 \u062a\u0648\u062c\u062f \u0628\u064a\u0627\u0646\u0627\u062a \u0645\u062a\u0627\u062d\u0629."],
    ];

    const patternReplacements = [
        {
            regex: /I found multiple parts for '([^']+)'\. Which one do you mean\?/gi,
            replacer: function (_m, part) { return "\u0648\u062c\u062f\u062a \u0639\u062f\u0629 \u0623\u0635\u0646\u0627\u0641 \u0644\u0640 '" + part + "'. \u0623\u064a \u0635\u0646\u0641 \u062a\u0642\u0635\u062f\u061f"; },
        },
        {
            regex: /No stock matched '([^']+)'\./gi,
            replacer: function (_m, q) { return "\u0644\u0645 \u064a\u062a\u0645 \u0627\u0644\u0639\u062b\u0648\u0631 \u0639\u0644\u0649 \u0645\u062e\u0632\u0648\u0646 \u0645\u0637\u0627\u0628\u0642 \u0644\u0640 '" + q + "'."; },
        },
        {
            regex: /No part matched '([^']+)'\. Try part number or clearer name\./gi,
            replacer: function (_m, q) { return "\u0644\u0627 \u064a\u0648\u062c\u062f \u0635\u0646\u0641 \u0645\u0637\u0627\u0628\u0642 \u0644\u0640 '" + q + "'. \u062c\u0631\u0651\u0628 \u0631\u0642\u0645 \u0627\u0644\u0635\u0646\u0641 \u0623\u0648 \u0627\u0633\u0645\u064b\u0627 \u0623\u0648\u0636\u062d."; },
        },
    ];

    function escapeRegExp(text) {
        return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    const sortedPhraseEntries = phraseEntries
        .slice()
        .sort(function (a, b) { return b[0].length - a[0].length; });

    function replaceWholePhrase(text, en, ar) {
        const escaped = escapeRegExp(en);
        const startsWord = /^[A-Za-z0-9]/.test(en);
        const endsWord = /[A-Za-z0-9]$/.test(en);
        let pattern = escaped;
        if (startsWord) {
            pattern = "(^|[^A-Za-z0-9])(" + pattern + ")";
        } else {
            pattern = "(" + pattern + ")";
        }
        if (endsWord) {
            pattern += "(?=$|[^A-Za-z0-9])";
        }
        const regex = new RegExp(pattern, "gi");

        if (startsWord) {
            return text.replace(regex, function (_m, lead) {
                return lead + ar;
            });
        }
        return text.replace(regex, ar);
    }

    function applyPatternReplacements(text) {
        let output = text;
        for (let i = 0; i < patternReplacements.length; i += 1) {
            const item = patternReplacements[i];
            output = output.replace(item.regex, item.replacer);
        }
        return output;
    }

    function applyPhraseMap(text) {
        let output = text;
        for (let i = 0; i < sortedPhraseEntries.length; i += 1) {
            const en = sortedPhraseEntries[i][0];
            const ar = sortedPhraseEntries[i][1];
            output = replaceWholePhrase(output, en, ar);
        }
        return output;
    }

    function translateValue(text) {
        if (!text || !/[A-Za-z]/.test(text)) return text;

        // Skip likely IDs/codes to avoid corrupting values like ORD-12345.
        if (/^[A-Za-z0-9][A-Za-z0-9_\-\/\.]*$/.test(text) && /[\d_\-\/\.]/.test(text)) {
            return text;
        }

        let output = text;
        output = applyPatternReplacements(output);
        output = applyPhraseMap(output);
        return output;
    }

    function shouldSkipNode(node) {
        if (!node || !node.parentElement) return true;
        const parent = node.parentElement;
        if (parent.closest("script, style, noscript")) return true;
        if (parent.tagName === "CODE" || parent.tagName === "PRE") return true;
        if (parent.closest("[data-no-force-ar]")) return true;
        return false;
    }

    function translateTextNodes(root) {
        if (!root) return;
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
        let current = walker.nextNode();
        while (current) {
            if (!shouldSkipNode(current)) {
                const original = current.nodeValue;
                const translated = translateValue(original);
                if (translated !== original) {
                    current.nodeValue = translated;
                }
            }
            current = walker.nextNode();
        }
    }

    function translateAttributes(root) {
        if (!root || !root.querySelectorAll) return;
        const selectors = ["[placeholder]", "[title]", "[aria-label]", "input[type='submit'][value]", "input[type='button'][value]", "input[type='reset'][value]"];
        root.querySelectorAll(selectors.join(",")).forEach(function (el) {
            if (el.closest("[data-no-force-ar]")) return;

            const attrs = ["placeholder", "title", "aria-label"];
            if (el.tagName === "INPUT") {
                const type = (el.getAttribute("type") || "").toLowerCase();
                if (type === "submit" || type === "button" || type === "reset") {
                    attrs.push("value");
                }
            }

            attrs.forEach(function (attr) {
                if (!el.hasAttribute(attr)) return;
                const original = el.getAttribute(attr) || "";
                const translated = translateValue(original);
                if (translated !== original) {
                    el.setAttribute(attr, translated);
                }
            });
        });
    }

    function translateRoot(root) {
        translateTextNodes(root);
        translateAttributes(root);
    }

    function runTranslation() {
        if (document && document.title) {
            document.title = translateValue(document.title);
        }
        translateRoot(document.body);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", runTranslation);
    } else {
        runTranslation();
    }

    const observer = new MutationObserver(function (mutations) {
        mutations.forEach(function (mutation) {
            mutation.addedNodes.forEach(function (node) {
                if (node.nodeType === 1) {
                    translateRoot(node);
                } else if (node.nodeType === 3 && node.parentElement) {
                    const translated = translateValue(node.nodeValue || "");
                    if (translated !== node.nodeValue) {
                        node.nodeValue = translated;
                    }
                }
            });
        });
    });

    observer.observe(document.documentElement, { childList: true, subtree: true });
})();
