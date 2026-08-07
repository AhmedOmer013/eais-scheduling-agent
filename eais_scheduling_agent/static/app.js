document.addEventListener("DOMContentLoaded", () => {
  // -- Tab switching --------------------------------------------------
  const tabButtons = document.querySelectorAll(".tab-button");
  const tabPanels = document.querySelectorAll(".tab-panel");

  function activateTab(tabId) {
    for (const button of tabButtons) {
      button.classList.toggle("active", button.dataset.tab === tabId);
    }
    for (const panel of tabPanels) {
      panel.classList.toggle("active", panel.id === tabId);
    }
    if (tabId === "tab-pending") loadPending();
    if (tabId === "tab-audit-clinic") {
      loadAudit("clinic");
      loadClinicRules();
    }
    if (tabId === "tab-audit-restaurant") {
      loadAudit("restaurant");
      loadRestaurantRules();
    }
  }

  for (const button of tabButtons) {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  }

  // Disables `button` and swaps its label to `loadingLabel` for the
  // duration of `action()`, restoring the original label afterward. Targets
  // a child `.btn-label` span when the button has one (so an icon inside
  // the button survives the swap); falls back to the whole button's text
  // otherwise -- unchanged behavior for icon-less buttons.
  async function withLoading(button, loadingLabel, action) {
    const labelEl = button.querySelector(".btn-label") || button;
    const originalLabel = labelEl.textContent;
    button.disabled = true;
    labelEl.textContent = loadingLabel;
    try {
      return await action();
    } finally {
      button.disabled = false;
      labelEl.textContent = originalLabel;
    }
  }

  function flashResult(el, text, statusClass) {
    el.textContent = text;
    el.className = "result";
    void el.offsetWidth;
    el.classList.add(statusClass, "visible");
  }

  // -- Booking form -----------------------------------------------------
  const bookingForm = document.getElementById("booking-form");
  const bookingResult = document.getElementById("booking-result");
  const sectorSelect = document.getElementById("sector");
  const textInput = document.getElementById("text");
  const useLlmCheckbox = document.getElementById("use-llm");

  const STATUS_MESSAGES = {
    CONFIRMED: (body) => ({ text: body.message, cls: "status-confirmed" }),
    PENDING_APPROVAL: (body) => ({
      text: `Sent for human review: ${body.reason}`,
      cls: "status-pending",
    }),
    NEEDS_CLARIFICATION: (body) => ({
      // body.reason is already a complete, friendly sentence built
      // server-side (see http_api.py's _build_clarification_message) --
      // no client-side string surgery needed here anymore.
      text: `We couldn't quite process that -- ${body.reason}`,
      cls: "status-clarify",
    }),
  };

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
        const renderer = STATUS_MESSAGES[body.status];
        const rendered = renderer
          ? renderer(body)
          : { text: `Unexpected status: ${body.status}`, cls: "status-error" };
        flashResult(bookingResult, rendered.text, rendered.cls);
      } else {
        flashResult(bookingResult, `Error: ${body.error}`, "status-error");
      }

      if (document.getElementById("tab-pending").classList.contains("active")) {
        loadPending();
      }
      refreshPendingBadge();
    });
  });

  // -- Pending queue ------------------------------------------------------
  const pendingList = document.getElementById("pending-list");
  const pendingBadge = document.getElementById("pending-badge");
  const refreshPendingButton = document.getElementById("refresh-pending");

  async function refreshPendingBadge() {
    const response = await fetch("/pending");
    const body = await response.json();
    pendingBadge.textContent = body.items.length;
  }

  function renderPendingCard(item) {
    const card = document.createElement("div");
    card.className = "pending-card";
    card.innerHTML = `
      <div class="meta"></div>
      <div class="text"></div>
      <div class="reason"></div>
      <div class="actions">
        <button type="button" class="accept">
          <svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 8.5L6.5 12L13 4.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span class="btn-label">Accept</span>
        </button>
        <button type="button" class="reject">
          <svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 4L12 12M12 4L4 12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          <span class="btn-label">Reject</span>
        </button>
      </div>
    `;
    card.querySelector(".meta").textContent = item.sector;
    card.querySelector(".text").textContent = `"${item.text}"`;
    card.querySelector(".reason").textContent = item.reason;

    card.querySelector(".accept").addEventListener("click", async (event) => {
      await withLoading(event.target, "Accepting...", async () => {
        const response = await fetch(`/pending/${item.id}/accept`, { method: "POST" });
        const body = await response.json();
        if (!response.ok) {
          if (response.status === 422) {
            alert(
              `Could not accept: ${body.error}\n\nAdd this practitioner/table under this sector's Slot rules card, then try Accept again.`
            );
          } else {
            alert(`Could not accept: ${body.error}`);
          }
        }
        await loadPending();
        await refreshPendingBadge();
      });
    });

    card.querySelector(".reject").addEventListener("click", async (event) => {
      await withLoading(event.target, "Rejecting...", async () => {
        const response = await fetch(`/pending/${item.id}/reject`, { method: "POST" });
        const body = await response.json();
        if (!response.ok) {
          alert(`Could not reject: ${body.error}`);
        }
        await loadPending();
        await refreshPendingBadge();
      });
    });

    return card;
  }

  async function loadPending() {
    const response = await fetch("/pending");
    const body = await response.json();
    pendingList.innerHTML = "";
    if (body.items.length === 0) {
      pendingList.innerHTML = '<p class="empty-state">Nothing pending.</p>';
      return;
    }
    for (const item of body.items) {
      pendingList.appendChild(renderPendingCard(item));
    }
  }

  refreshPendingButton.addEventListener("click", () => {
    withLoading(refreshPendingButton, "Refreshing...", loadPending);
  });

  // -- Audit tabs (per sector) --------------------------------------------
  async function loadAudit(sector) {
    const response = await fetch(`/audit?sector=${sector}`);
    const body = await response.json();
    const tbody = document.getElementById(`audit-body-${sector}`);
    tbody.innerHTML = "";
    for (const record of body.records) {
      const row = document.createElement("tr");
      row.innerHTML = "<td></td><td></td><td></td><td></td>";
      row.children[0].textContent = record.timestamp;
      row.children[1].textContent = record.input;
      row.children[2].textContent = record.decision;
      row.children[3].textContent = record.approval_status;
      tbody.appendChild(row);
    }
  }

  for (const button of document.querySelectorAll(".refresh-audit")) {
    button.addEventListener("click", () => {
      withLoading(button, "Refreshing...", () => loadAudit(button.dataset.sector));
    });
  }

  // -- Slot rules (per sector) ---------------------------------------------
  // `onDelete(name)` is called when that row's Delete button is clicked --
  // the caller decides which sector/endpoint that maps to. The "Hours" row
  // has no delete button: working hours are always required, so "deleting"
  // them makes no sense -- changing them is what the edit form is for.
  function renderRulesDisplay(container, items, workingHours, onDelete) {
    container.innerHTML = "";
    for (const [name, value] of Object.entries(items)) {
      const row = document.createElement("div");
      row.className = "rules-row";
      row.innerHTML =
        '<span class="label"></span><span class="value"></span>' +
        '<button type="button" class="delete-item">' +
        '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 4.5H13M6.5 4.5V3a1 1 0 011-1h1a1 1 0 011 1v1.5M6.5 7.5V12M9.5 7.5V12M4.5 4.5L5 13a1 1 0 001 1h4a1 1 0 001-1l.5-8.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
        '<span class="btn-label">Delete</span>' +
        "</button>";
      row.querySelector(".label").textContent = name;
      row.querySelector(".value").textContent = value;
      const deleteButton = row.querySelector(".delete-item");
      deleteButton.addEventListener("click", () => {
        withLoading(deleteButton, "...", () => onDelete(name));
      });
      container.appendChild(row);
    }

    const hoursRow = document.createElement("div");
    hoursRow.className = "rules-row";
    hoursRow.innerHTML = '<span class="label">Hours</span><span class="value"></span>';
    hoursRow.querySelector(".value").textContent = `${workingHours.open}–${workingHours.close}`;
    container.appendChild(hoursRow);
  }

  async function deleteClinicPractitioner(name) {
    const response = await fetch("/config/clinic", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remove_practitioners: [name] }),
    });
    if (!response.ok) {
      const body = await response.json();
      alert(`Could not remove: ${body.error}`);
    }
    await loadClinicRules();
  }

  async function deleteRestaurantTable(tableId) {
    const response = await fetch("/config/restaurant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ remove_tables: [tableId] }),
    });
    if (!response.ok) {
      const body = await response.json();
      alert(`Could not remove: ${body.error}`);
    }
    await loadRestaurantRules();
  }

  async function loadClinicRules() {
    const response = await fetch("/config/clinic");
    const body = await response.json();
    const items = {};
    for (const [name, minutes] of Object.entries(body.practitioners)) {
      items[name] = `${minutes} min`;
    }
    renderRulesDisplay(
      document.getElementById("clinic-rules-display"),
      items,
      body.working_hours,
      deleteClinicPractitioner
    );
  }

  async function loadRestaurantRules() {
    const response = await fetch("/config/restaurant");
    const body = await response.json();
    const items = {};
    for (const [tableId, capacity] of Object.entries(body.tables)) {
      items[tableId] = `${capacity} seats`;
    }
    renderRulesDisplay(
      document.getElementById("restaurant-rules-display"),
      items,
      body.working_hours,
      deleteRestaurantTable
    );
  }

  for (const toggle of document.querySelectorAll(".toggle-edit")) {
    toggle.addEventListener("click", () => {
      document.getElementById(toggle.dataset.target).classList.toggle("hidden");
    });
  }

  const clinicRulesForm = document.getElementById("clinic-rules-form");
  const clinicRulesResult = document.getElementById("clinic-rules-result");

  clinicRulesForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = clinicRulesForm.querySelector("button[type=submit]");
    const name = document.getElementById("clinic-practitioner-name").value.trim();
    const duration = document.getElementById("clinic-practitioner-duration").value;
    const open = document.getElementById("clinic-open").value.trim();
    const close = document.getElementById("clinic-close").value.trim();

    if ((open !== "") !== (close !== "")) {
      flashResult(clinicRulesResult, "Enter both open and close, or leave both blank.", "status-error");
      return;
    }

    const payload = {};
    if (name !== "" && duration !== "") {
      payload.practitioners = { [name]: Number(duration) };
    }
    if (open !== "" && close !== "") {
      payload.working_hours = { open, close };
    }

    await withLoading(submitButton, "Saving...", async () => {
      const response = await fetch("/config/clinic", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();

      if (response.ok) {
        flashResult(clinicRulesResult, "Saved.", "status-confirmed");
        clinicRulesForm.reset();
        await loadClinicRules();
      } else {
        flashResult(clinicRulesResult, `Error: ${body.error}`, "status-error");
      }
    });
  });

  const restaurantRulesForm = document.getElementById("restaurant-rules-form");
  const restaurantRulesResult = document.getElementById("restaurant-rules-result");

  restaurantRulesForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitButton = restaurantRulesForm.querySelector("button[type=submit]");
    const tableId = document.getElementById("restaurant-table-id").value.trim();
    const capacity = document.getElementById("restaurant-table-capacity").value;
    const open = document.getElementById("restaurant-open").value.trim();
    const close = document.getElementById("restaurant-close").value.trim();

    if ((open !== "") !== (close !== "")) {
      flashResult(
        restaurantRulesResult,
        "Enter both open and close, or leave both blank.",
        "status-error"
      );
      return;
    }

    const payload = {};
    if (tableId !== "" && capacity !== "") {
      payload.tables = { [tableId]: Number(capacity) };
    }
    if (open !== "" && close !== "") {
      payload.working_hours = { open, close };
    }

    await withLoading(submitButton, "Saving...", async () => {
      const response = await fetch("/config/restaurant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();

      if (response.ok) {
        flashResult(restaurantRulesResult, "Saved.", "status-confirmed");
        restaurantRulesForm.reset();
        await loadRestaurantRules();
      } else {
        flashResult(restaurantRulesResult, `Error: ${body.error}`, "status-error");
      }
    });
  });

  // -- Config -------------------------------------------------------------
  const configForm = document.getElementById("config-form");
  const configResult = document.getElementById("config-result");
  const baseUrlInput = document.getElementById("base-url");
  const modelInput = document.getElementById("model");
  const apiKeyInput = document.getElementById("api-key");
  const apiKeyHint = document.getElementById("api-key-hint");
  const timeoutInput = document.getElementById("timeout");

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
        flashResult(configResult, "Saved.", "status-confirmed");
        apiKeyInput.value = "";
        await loadConfig();
      } else {
        flashResult(configResult, `Error: ${body.error}`, "status-error");
      }
    });
  });

  refreshPendingBadge();
  loadConfig();
});
