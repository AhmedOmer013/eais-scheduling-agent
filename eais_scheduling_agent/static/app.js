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

  bookingForm.addEventListener("submit", async (event) => {
    event.preventDefault();
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
      bookingResult.textContent =
        body.status === "CONFIRMED" ? body.message : `Pending approval: ${body.reason}`;
    } else {
      bookingResult.textContent = `Error: ${body.error}`;
    }

    loadAudit();
  });

  async function loadAudit() {
    const response = await fetch("/audit");
    const body = await response.json();
    auditBody.innerHTML = "";
    for (const record of body.records) {
      const row = document.createElement("tr");
      row.innerHTML =
        "<td></td><td></td><td></td><td></td>";
      row.children[0].textContent = record.timestamp;
      row.children[1].textContent = record.skill_pack;
      row.children[2].textContent = record.input;
      row.children[3].textContent = record.decision;
      auditBody.appendChild(row);
    }
  }

  refreshAuditButton.addEventListener("click", loadAudit);

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
      configResult.textContent = "Saved.";
      apiKeyInput.value = "";
      loadConfig();
    } else {
      configResult.textContent = `Error: ${body.error}`;
    }
  });

  loadAudit();
  loadConfig();
});
