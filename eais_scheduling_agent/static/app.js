document.addEventListener("DOMContentLoaded", () => {
  const bookingForm = document.getElementById("booking-form");
  const bookingResult = document.getElementById("booking-result");
  const sectorSelect = document.getElementById("sector");
  const textInput = document.getElementById("text");
  const useLlmCheckbox = document.getElementById("use-llm");
  const auditBody = document.getElementById("audit-body");
  const refreshAuditButton = document.getElementById("refresh-audit");
  const configForm = document.getElementById("config-form");
  const configResult = document.getElementById("config-result");
  const baseUrlInput = document.getElementById("base-url");
  const modelInput = document.getElementById("model");
  const apiKeyInput = document.getElementById("api-key");
  const apiKeyHint = document.getElementById("api-key-hint");
  const timeoutInput = document.getElementById("timeout");

  // Disables `button` and swaps its label to `loadingLabel` for the
  // duration of `action()`, restoring the original label afterward --
  // purely a visual loading cue, no behavior change to the request itself.
  async function withLoading(button, loadingLabel, action) {
    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = loadingLabel;
    try {
      return await action();
    } finally {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  }

  // Sets `el`'s text and (re)triggers its fade-in transition, even if the
  // same element already held that class from a previous call -- forcing
  // a reflow between removing and re-adding "visible" is what makes the
  // opacity transition replay on every result, not just the first one.
  function flashResult(el, text) {
    el.textContent = text;
    el.classList.remove("visible");
    void el.offsetWidth;
    el.classList.add("visible");
  }

  bookingForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = bookingForm.querySelector("button[type=submit]");

    await withLoading(submitButton, "Booking...", async () => {
      const response = await fetch("/bookings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sector: sectorSelect.value,
          text: textInput.value,
          llm: useLlmCheckbox.checked,
        }),
      });
      const body = await response.json();

      if (response.ok) {
        flashResult(
          bookingResult,
          body.status === "CONFIRMED" ? body.message : `Pending approval: ${body.reason}`
        );
      } else {
        flashResult(bookingResult, `Error: ${body.error}`);
      }

      await loadAudit();
    });
  });

  async function loadAudit() {
    const response = await fetch("/audit");
    const body = await response.json();
    auditBody.innerHTML = "";
    for (const record of body.records) {
      const row = document.createElement("tr");
      row.className = "audit-row";
      row.innerHTML =
        "<td></td><td></td><td></td><td></td>";
      row.children[0].textContent = record.timestamp;
      row.children[1].textContent = record.skill_pack;
      row.children[2].textContent = record.input;
      row.children[3].textContent = record.decision;
      auditBody.appendChild(row);
    }
  }

  refreshAuditButton.addEventListener("click", () => {
    withLoading(refreshAuditButton, "Refreshing...", loadAudit);
  });

  async function loadConfig() {
    const response = await fetch("/config");
    const body = await response.json();
    baseUrlInput.value = body.base_url;
    modelInput.value = body.model;
    timeoutInput.value = body.timeout;
    apiKeyHint.textContent = body.api_key_set
      ? "(already set -- leave blank to keep)"
      : "(not set)";
  }

  configForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = configForm.querySelector("button[type=submit]");

    await withLoading(submitButton, "Saving...", async () => {
      const payload = {
        base_url: baseUrlInput.value,
        model: modelInput.value,
        timeout: timeoutInput.value === "" ? null : Number(timeoutInput.value),
      };
      if (apiKeyInput.value !== "") {
        payload.api_key = apiKeyInput.value;
      }

      const response = await fetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();

      if (response.ok) {
        flashResult(configResult, "Saved.");
        apiKeyInput.value = "";
        await loadConfig();
      } else {
        flashResult(configResult, `Error: ${body.error}`);
      }
    });
  });

  loadAudit();
  loadConfig();
});
