(function () {
    const DUPLICATE_DEBOUNCE_MS = 2000;

    const modalEl = document.getElementById("barcodeScannerModal");
    const containerEl = document.getElementById("barcodeScannerContainer");
    const statusEl = document.getElementById("barcodeScannerStatus");
    const closeButton = document.querySelector("[data-barcode-camera-close]");
    const switchButton = document.querySelector("[data-barcode-camera-switch]");

    if (!modalEl || !containerEl || !statusEl || !window.bootstrap) {
        return;
    }

    const libScript = document.querySelector("script[data-barcode-lib]");
    const libPath = (libScript && libScript.getAttribute("src")) || "(missing script tag)";
    const hasLibrary = typeof window.Html5Qrcode === "function";
    console.info("[DELTA Scanner] library check", { hasLibrary: hasLibrary, path: libPath });

    const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
    let html5QrCode = null;
    let isScanning = false;
    let isStarting = false;
    let currentTargetInput = null;
    let currentForm = null;
    let autoSubmit = true;
    let lastScanValue = "";
    let lastScanAt = 0;
    let cameraDevices = [];
    let currentCameraIndex = 0;
    let duplicatePromptEl = null;

    function setStatus(message, variant) {
        statusEl.textContent = message;
        statusEl.classList.remove("text-warning", "text-danger", "text-success", "text-light-emphasis");
        if (variant === "warning") statusEl.classList.add("text-warning");
        else if (variant === "danger") statusEl.classList.add("text-danger");
        else if (variant === "success") statusEl.classList.add("text-success");
        else statusEl.classList.add("text-light-emphasis");
    }

    function showFlash(message, level) {
        const box = document.createElement("div");
        box.className = "alert alert-" + (level || "success") + " shadow-sm";
        box.setAttribute("role", "status");
        box.style.position = "fixed";
        box.style.top = "1rem";
        box.style.left = "50%";
        box.style.transform = "translateX(-50%)";
        box.style.zIndex = "2000";
        box.style.minWidth = "260px";
        box.style.maxWidth = "88vw";
        box.textContent = message;
        document.body.appendChild(box);
        window.setTimeout(function () { box.remove(); }, 1800);
    }

    function clearDuplicatePrompt() {
        if (duplicatePromptEl) {
            duplicatePromptEl.remove();
            duplicatePromptEl = null;
        }
    }

    function showDuplicatePrompt(message, onIncrease, onIgnore) {
        clearDuplicatePrompt();

        const panel = document.createElement("div");
        panel.className = "alert alert-warning shadow";
        panel.style.position = "fixed";
        panel.style.bottom = "1rem";
        panel.style.left = "50%";
        panel.style.transform = "translateX(-50%)";
        panel.style.zIndex = "2100";
        panel.style.minWidth = "300px";
        panel.style.maxWidth = "92vw";

        const text = document.createElement("div");
        text.className = "mb-2";
        text.textContent = message;

        const actions = document.createElement("div");
        actions.className = "d-flex gap-2 justify-content-end";

        const increaseBtn = document.createElement("button");
        increaseBtn.type = "button";
        increaseBtn.className = "btn btn-sm btn-primary";
        increaseBtn.textContent = "زيادة";

        const ignoreBtn = document.createElement("button");
        ignoreBtn.type = "button";
        ignoreBtn.className = "btn btn-sm btn-outline-secondary";
        ignoreBtn.textContent = "تجاهل";

        increaseBtn.addEventListener("click", function () {
            clearDuplicatePrompt();
            if (typeof onIncrease === "function") onIncrease();
        });
        ignoreBtn.addEventListener("click", function () {
            clearDuplicatePrompt();
            if (typeof onIgnore === "function") onIgnore();
        });

        actions.appendChild(ignoreBtn);
        actions.appendChild(increaseBtn);
        panel.appendChild(text);
        panel.appendChild(actions);
        document.body.appendChild(panel);
        duplicatePromptEl = panel;
    }

    function updateSwitchButton() {
        if (!switchButton) return;
        const canSwitch = cameraDevices.length > 1;
        switchButton.style.display = canSwitch ? "inline-block" : "none";
        switchButton.disabled = isStarting || !canSwitch;
    }

    function preferredCameraIndex(cameras) {
        for (let i = 0; i < cameras.length; i += 1) {
            const label = String(cameras[i].label || "").toLowerCase();
            if (label.includes("back") || label.includes("rear") || label.includes("environment")) {
                return i;
            }
        }
        return 0;
    }

    async function ensureCamerasLoaded() {
        if (cameraDevices.length > 0) {
            updateSwitchButton();
            return;
        }
        if (typeof window.Html5Qrcode !== "function") {
            return;
        }
        try {
            const cameras = await window.Html5Qrcode.getCameras();
            cameraDevices = Array.isArray(cameras)
                ? cameras.filter(function (camera) { return camera && camera.id; })
                : [];
            if (cameraDevices.length > 0) {
                currentCameraIndex = preferredCameraIndex(cameraDevices);
            }
        } catch (_) {
            cameraDevices = [];
        }
        updateSwitchButton();
    }

    async function stopScanner() {
        clearDuplicatePrompt();
        if (!html5QrCode) {
            containerEl.innerHTML = "";
            isScanning = false;
            return;
        }

        if (isScanning) {
            try {
                await html5QrCode.stop();
            } catch (_) {}
        }

        try {
            await html5QrCode.clear();
        } catch (_) {}

        html5QrCode = null;
        isScanning = false;
        containerEl.innerHTML = "";
    }

    function resolveTarget(trigger) {
        const targetSelector = trigger.getAttribute("data-barcode-scan-target");
        const formSelector = trigger.getAttribute("data-barcode-form");
        autoSubmit = (trigger.getAttribute("data-barcode-auto-submit") || "true").toLowerCase() !== "false";

        currentTargetInput = targetSelector ? document.querySelector(targetSelector) : null;
        if (!currentTargetInput) return false;

        currentForm = formSelector ? document.querySelector(formSelector) : currentTargetInput.closest("form");
        return true;
    }

    function applyScanValue(value) {
        const cleanValue = (value || "").trim();
        if (!cleanValue || !currentTargetInput) return;

        currentTargetInput.value = cleanValue;
        currentTargetInput.dispatchEvent(new Event("input", { bubbles: true }));
        currentTargetInput.dispatchEvent(new Event("change", { bubbles: true }));

        showFlash("تم مسح الباركود بنجاح: " + cleanValue, "success");

        if (autoSubmit && currentForm) {
            window.setTimeout(function () {
                if (typeof currentForm.requestSubmit === "function") currentForm.requestSubmit();
                else currentForm.submit();
            }, 80);
        }
    }

    async function commitDecoded(value) {
        setStatus("Barcode detected. Applying...", "success");
        applyScanValue(value);
        await stopScanner();
        modal.hide();
    }

    async function handleDecodeResult(text) {
        const value = (text || "").trim();
        if (!value) return;

        const now = Date.now();
        if (value === lastScanValue && now - lastScanAt < DUPLICATE_DEBOUNCE_MS) {
            showDuplicatePrompt(
                "تم مسح نفس الصنف للتو — هل تريد زيادة الكمية؟",
                function () {
                    lastScanAt = Date.now();
                    commitDecoded(value);
                },
                function () {
                    setStatus("Duplicate scan ignored.", "warning");
                }
            );
            return;
        }

        lastScanValue = value;
        lastScanAt = now;
        await commitDecoded(value);
    }

    async function startScanner() {
        if (isStarting) return;
        isStarting = true;
        updateSwitchButton();
        try {
            if (!window.isSecureContext) {
                throw new Error("Camera access requires HTTPS (or localhost).");
            }
            if (typeof window.Html5Qrcode !== "function") {
                throw new Error("Could not load barcode scanner library from: " + libPath);
            }

            await ensureCamerasLoaded();
            setStatus("Requesting camera access...", null);
            await stopScanner();

            html5QrCode = new window.Html5Qrcode("barcodeScannerContainer", {
                useBarCodeDetectorIfSupported: false
            });

            const qrboxSize = Math.max(180, Math.min(300, Math.floor((window.innerWidth || 360) * 0.62)));
            const cameraConfig = cameraDevices.length > 0
                ? { deviceId: { exact: cameraDevices[currentCameraIndex].id } }
                : { facingMode: "environment" };

            await html5QrCode.start(
                cameraConfig,
                { fps: 10, qrbox: qrboxSize },
                function (decodedText) {
                    handleDecodeResult(decodedText);
                },
                function () {
                    // Ignore per-frame decode errors.
                }
            );

            isScanning = true;
            setStatus("Camera active. Point at barcode...", null);
        } catch (error) {
            const rawMessage = (error && error.message) ? error.message : "Unable to start camera scanner.";
            const lower = rawMessage.toLowerCase();
            let message = rawMessage;
            if (lower.includes("notallowed") || lower.includes("permission") || lower.includes("denied")) {
                message = "تم رفض إذن الكاميرا / Camera permission denied. استخدم الماسح اليدوي أو اكتب الكود.";
            } else if (lower.includes("notfound") || lower.includes("no camera")) {
                message = "لا توجد كاميرا متاحة / No camera device was found.";
            }
            setStatus(message, "danger");
            showFlash(message, "danger");
            console.error("[DELTA Scanner] start error:", rawMessage);
            await stopScanner();
        } finally {
            isStarting = false;
            updateSwitchButton();
        }
    }

    function openScanner(trigger) {
        if (!resolveTarget(trigger)) {
            showFlash("Scanner target input not found.", "danger");
            return;
        }
        lastScanValue = "";
        lastScanAt = 0;
        setStatus("افتح الكاميرا ووجّهها نحو الباركود.", null);
        modal.show();
        window.setTimeout(function () { startScanner(); }, 200);
    }

    document.addEventListener("click", function (event) {
        const trigger = event.target.closest("[data-barcode-scan-target]");
        if (!trigger) return;
        event.preventDefault();
        openScanner(trigger);
    });

    if (closeButton) {
        closeButton.addEventListener("click", function () { modal.hide(); });
    }

    if (switchButton) {
        switchButton.addEventListener("click", async function () {
            if (cameraDevices.length <= 1 || isStarting) return;
            currentCameraIndex = (currentCameraIndex + 1) % cameraDevices.length;
            await startScanner();
        });
    }

    modalEl.addEventListener("shown.bs.modal", function () {
        ensureCamerasLoaded();
    });

    modalEl.addEventListener("hidden.bs.modal", function () {
        stopScanner();
        currentTargetInput = null;
        currentForm = null;
        setStatus("Scanner closed.", null);
    });

    window.deltaAttachRepeatGuard = function (form, input) {
        if (!form || !input || form.dataset.scanGuardBound === "1") return;
        form.dataset.scanGuardBound = "1";

        let lastToken = "";
        let lastAt = 0;
        let approvedToken = "";

        form.addEventListener("submit", function (event) {
            const token = String(input.value || "").trim();
            if (!token) return;

            const now = Date.now();
            const sameRecent = token === lastToken && (now - lastAt) <= DUPLICATE_DEBOUNCE_MS;
            if (sameRecent && approvedToken !== token) {
                event.preventDefault();
                showDuplicatePrompt(
                    "تم مسح نفس الصنف للتو — هل تريد زيادة الكمية؟",
                    function () {
                        approvedToken = token;
                        if (typeof form.requestSubmit === "function") form.requestSubmit();
                        else form.submit();
                    },
                    function () {
                        approvedToken = "";
                        input.focus();
                    }
                );
                return;
            }

            approvedToken = "";
            lastToken = token;
            lastAt = now;
        });
    };
})();
