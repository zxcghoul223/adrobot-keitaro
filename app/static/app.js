"use strict";
(() => {
  const byId = (id) => document.getElementById(id);
  const state = {
    settings: null,
    catalog: null,
    campaign: null,
    streamId: null,
    busy: false,
    createdId: null,
    searchTimer: null,
    listRequest: 0,
    campaignRequest: 0,
  };
  const numberFormat = new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 4,
  });
  function message(id, text = "") {
    const node = byId(id);
    node.textContent = text;
    node.hidden = !text;
  }
  function errorText(error) {
    if (/failed to fetch|networkerror|load failed/i.test(error?.message || "")) {
      return "Нет связи с приложением. Проверьте, что сервер запущен, и повторите запрос.";
    }
    return error?.message || "Не удалось выполнить запрос. Повторите попытку.";
  }
  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error(
        `Сервер вернул некорректный ответ (HTTP ${response.status}).`,
      );
    }
    if (!response.ok) {
      const detail = data.detail;
      const description = Array.isArray(detail)
        ? detail
            .map(
              (item) =>
                `${(item.loc || []).filter((part) => part !== "body").join(".")}: ${item.msg}`,
            )
            .join("; ")
        : typeof detail === "string"
          ? detail
          : data.message;
      const error = new Error(
        description || `Ошибка запроса (HTTP ${response.status}).`,
      );
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function setTab(tab) {
    for (const name of ["create", "editor"]) {
      const active = name === tab;
      byId(`tab-${name}`).classList.toggle("active", active);
      byId(`tab-${name}`).setAttribute("aria-selected", String(active));
      byId(`${name}-panel`).hidden = !active;
    }
  }
  function fillSelect(id, items, emptyLabel) {
    const select = byId(id);
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = items.length ? "Выберите значение" : emptyLabel;
    select.append(placeholder);
    for (const item of items) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.name || item.domain || `ID ${item.id}`;
      select.append(option);
    }
    if (items.length === 1) select.value = String(items[0].id);
  }
  function offerLabel(offer) {
    return `${offer.id} — ${offer.name}`;
  }
  function resolveOffer(value) {
    const text = value.trim();
    if (!text) return null;
    return (
      state.catalog.offers.find(
        (offer) =>
          offerLabel(offer) === text ||
          String(offer.id) === text ||
          offer.name.toLocaleLowerCase() === text.toLocaleLowerCase(),
      ) || null
    );
  }
  function countryCode(value) {
    const text = value.trim().toUpperCase();
    return (
      state.catalog.countries.find(
        (country) =>
          country.code === text ||
          `${country.code} — ${country.name}`.toUpperCase() === text ||
          country.name.toUpperCase() === text,
      )?.code || null
    );
  }
  function setSafeLink(id, value) {
    const link = byId(id);
    link.hidden = true;
    link.removeAttribute("href");
    link.textContent = "";
    if (!value) return;
    try {
      const url = new URL(value);
      if (!["http:", "https:"].includes(url.protocol)) return;
      link.href = url.href;
      link.textContent = url.href;
      link.hidden = false;
    } catch {
      /* An unavailable URL does not prevent editing. */
    }
  }
  function showGlobalError(error) {
    byId("global-error-text").textContent = errorText(error);
    byId("global-error").hidden = false;
  }
  async function initialize() {
    byId("create-fields").disabled = true;
    byId("global-error").hidden = true;
    const connection = document.querySelector(".connection-label");
    connection.classList.remove("failed", "connected");
    byId("connection-label").textContent = "Проверяем соединение";
    try {
      const [settings, catalog] = await Promise.all([
        request("/api/settings"),
        request("/api/catalog"),
      ]);
      state.settings = settings;
      state.catalog = catalog;
      for (const field of [
        "domains",
        "groups",
        "traffic_sources",
        "offers",
        "countries",
      ])
        if (!Array.isArray(catalog[field]))
          throw new Error("Не удалось получить справочники Keitaro.");
      const live = settings.mode === "live";
      byId("mode-badge").textContent = live
        ? "KEITARO · LIVE"
        : "DEMO · ЛОКАЛЬНО";
      byId("mode-badge").classList.toggle("live", live);
      byId("demo-notice").hidden = live;
      byId("connection-label").textContent = live
        ? "Keitaro подключён"
        : "Демо готово к работе";
      connection.classList.add("connected");
      byId("campaign-prefix").textContent = settings.campaign_prefix;
      fillSelect("domain-id", catalog.domains, "Нет доступных доменов");
      fillSelect("group-id", catalog.groups, "Нет доступных групп");
      fillSelect(
        "traffic-source-id",
        catalog.traffic_sources,
        "Нет доступных источников",
      );
      byId("offers-list").replaceChildren(
        ...catalog.offers.map((offer) => {
          const option = document.createElement("option");
          option.value = offerLabel(offer);
          return option;
        }),
      );
      byId("countries-list").replaceChildren(
        ...catalog.countries.map((country) => {
          const option = document.createElement("option");
          option.value = `${country.code} — ${country.name}`;
          return option;
        }),
      );
      byId("create-fields").disabled = false;
      await loadCampaigns();
    } catch (error) {
      byId("mode-badge").textContent = "НЕТ СОЕДИНЕНИЯ";
      byId("connection-label").textContent = "Подключение недоступно";
      connection.classList.add("failed");
      showGlobalError(error);
    }
  }
  async function loadCampaigns() {
    const token = ++state.listRequest;
    byId("refresh-campaigns").disabled = true;
    byId("campaign-count").textContent = "Загрузка…";
    try {
      const data = await request(
        `/api/campaigns?search=${encodeURIComponent(byId("campaign-search").value.trim())}`,
      );
      if (token !== state.listRequest) return;
      const list = byId("campaign-list");
      list.replaceChildren();
      byId("campaign-count").textContent = `Найдено: ${data.items.length}`;
      for (const campaign of data.items) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "campaign-list-item";
        button.dataset.campaignId = campaign.id;
        const selected = String(state.campaign?.id) === String(campaign.id);
        button.classList.toggle("active", selected);
        button.setAttribute("aria-pressed", String(selected));
        const title = document.createElement("strong");
        title.textContent = campaign.name;
        const meta = document.createElement("small");
        meta.textContent = `Кампания #${campaign.id}`;
        button.append(title, meta);
        button.addEventListener("click", () => loadCampaign(campaign.id));
        list.append(button);
      }
      if (!data.items.length) {
        const empty = document.createElement("p");
        empty.className = "list-empty";
        empty.textContent = byId("campaign-search").value.trim()
          ? "Кампании не найдены. Попробуйте другое название."
          : "Здесь появятся доступные кампании. Начните с создания новой.";
        list.append(empty);
      }
    } catch (error) {
      if (token === state.listRequest) {
        byId("campaign-count").textContent = "Не удалось загрузить список";
        showGlobalError(error);
      }
    } finally {
      if (token === state.listRequest)
        byId("refresh-campaigns").disabled = false;
    }
  }
  function activeStream() {
    return state.campaign?.streams.find(
      (stream) => String(stream.id) === String(state.streamId),
    );
  }
  function streamDirty(stream) {
    return Boolean(stream?.dirty);
  }
  function campaignDirty() {
    return (
      Boolean(state.campaign?.dirty) ||
      Boolean(state.campaign?.streams.some(streamDirty))
    );
  }
  async function loadCampaign(id) {
    if (state.busy) return;
    const token = ++state.campaignRequest;
    byId("editor-content").classList.add("loading-opacity");
    message("editor-error");
    byId("editor-success").hidden = true;
    try {
      const campaign = await request(
        `/api/campaigns/${encodeURIComponent(id)}`,
      );
      if (token !== state.campaignRequest) return;
      state.campaign = campaign;
      state.streamId =
        campaign.streams.find((stream) => !stream.read_only)?.id ??
        campaign.streams[0]?.id ??
        null;
      renderCampaign();
    } catch (error) {
      if (token === state.campaignRequest) showGlobalError(error);
    } finally {
      if (token === state.campaignRequest)
        byId("editor-content").classList.remove("loading-opacity");
    }
  }
  function renderCampaign() {
    const campaign = state.campaign;
    if (!campaign) return;
    byId("editor-placeholder").hidden = true;
    byId("editor-content").hidden = false;
    byId("editor-campaign-id").textContent = `КАМПАНИЯ #${campaign.id}`;
    byId("editor-campaign-name").textContent = campaign.name;
    setSafeLink("editor-campaign-url", campaign.url);
    byId("draft-badge").hidden = !campaignDirty();
    byId("fetch-streams-button").disabled = state.busy;
    for (const button of byId("campaign-list").querySelectorAll("button")) {
      const selected = button.dataset.campaignId === String(campaign.id);
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    }
    const streams = byId("streams-list");
    streams.replaceChildren();
    for (const stream of campaign.streams) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "stream-tab";
      const selected = String(stream.id) === String(state.streamId);
      button.classList.toggle("active", selected);
      button.classList.toggle("dirty", streamDirty(stream));
      button.setAttribute("aria-pressed", String(selected));
      button.disabled = state.busy;
      const title = document.createElement("span");
      title.textContent = stream.name || `Поток #${stream.id}`;
      const note = document.createElement("small");
      note.textContent = stream.read_only
        ? "Без офферов"
        : streamDirty(stream)
          ? "Не опубликован"
          : "Офферы";
      button.append(title, note);
      button.addEventListener("click", () => {
        if (state.busy || selected) return;
        state.streamId = stream.id;
        message("editor-error");
        byId("editor-success").hidden = true;
        renderCampaign();
      });
      streams.append(button);
    }
    const stream = activeStream();
    const readOnly = !stream || Boolean(stream.read_only);
    byId("readonly-notice").hidden = !readOnly;
    byId("offers-editor").hidden = readOnly;
    byId("editor-content").classList.toggle("has-draft", streamDirty(stream));
    if (!readOnly) renderOffers();
  }
  function renderOffers() {
    const stream = activeStream();
    if (!stream) return;
    message(
      "stream-warning",
      stream.warning
        ? `${String(stream.warning)} Измените состав офферов или закреплённые доли, чтобы пересчитать распределение.`
        : "",
    );
    const body = byId("offers-body");
    body.replaceChildren();
    const allOffers = stream.offers || [];
    const active = allOffers.filter((offer) => offer.state !== "removed");
    const visible = allOffers.filter(
      (offer) => byId("show-archive").checked || offer.state !== "removed",
    );
    const total = active.reduce(
      (sum, offer) => sum + Number(offer.share || 0),
      0,
    );
    for (const offer of visible) {
      const id = Number(offer.offer_id);
      const removed = offer.state === "removed";
      const row = document.createElement("tr");
      row.classList.toggle("archived-offer", removed);
      const nameCell = document.createElement("td");
      const name = document.createElement("span");
      name.className = "offer-name";
      name.textContent =
        offer.name ||
        state.catalog.offers.find((item) => Number(item.id) === id)?.name ||
        `Оффер #${id}`;
      const meta = document.createElement("span");
      meta.className = "offer-id";
      meta.textContent = `ID ${id}${removed ? " · removed / архив" : ""}${offer.remote_disabled || offer.keitaro_state === "disabled" ? " · отключён в Keitaro" : ""}${offer.pinned ? " · доля закреплена" : ""}`;
      nameCell.append(name, meta);
      const shareCell = document.createElement("td");
      shareCell.className = "number-cell preview-cell";
      if (offer.pinned && !removed) {
        const input = document.createElement("input");
        input.type = "number";
        input.className = "share-input";
        input.value = Number(offer.share);
        input.min = "0";
        input.max = "100";
        input.step = "1";
        input.disabled = state.busy;
        input.setAttribute(
          "aria-label",
          `Закреплённая доля оффера ${id}, процентов`,
        );
        input.title =
          "Целое число процентов. Нажмите Enter или перейдите к следующему полю, чтобы применить.";
        const commitShare = () => {
          if (state.busy) return;
          if (!input.checkValidity() || input.value.trim() === "") {
            input.reportValidity();
            input.value = Number(offer.share);
            return;
          }
          const share = Number(input.value);
          if (share !== Number(offer.share)) mutateDraft("pin", id, share);
        };
        input.addEventListener("change", commitShare);
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commitShare();
          }
        });
        shareCell.append(input);
      } else
        shareCell.textContent = `${numberFormat.format(removed ? 0 : Number(offer.share || 0))}%`;
      const pinCell = document.createElement("td");
      pinCell.className = "pin-cell";
      if (!removed) {
        const pin = document.createElement("input");
        pin.type = "checkbox";
        pin.className = "pin-checkbox";
        pin.checked = Boolean(offer.pinned);
        pin.disabled = state.busy;
        pin.setAttribute("aria-label", `Закрепить долю оффера ${id}`);
        pin.title =
          "Закреплённая доля не меняется при добавлении и удалении других офферов";
        pin.addEventListener("change", () =>
          mutateDraft(pin.checked ? "pin" : "unpin", id, Number(offer.share)),
        );
        pinCell.append(pin);
      } else pinCell.textContent = offer.pinned ? "●" : "—";
      const actions = document.createElement("td");
      const action = document.createElement("button");
      action.type = "button";
      action.className = removed
        ? "offer-action restore-action"
        : "offer-action remove-action";
      action.textContent = removed ? "Bring back" : "Remove";
      action.title = removed
        ? "Вернуть оффер из архива в черновик"
        : "Убрать оффер из потока, сохранив его в архиве";
      action.setAttribute(
        "aria-label",
        `${removed ? "Вернуть" : "Удалить"} оффер ${id}`,
      );
      action.disabled = state.busy;
      action.addEventListener("click", () =>
        mutateDraft(removed ? "restore" : "remove", id),
      );
      actions.append(action);
      row.append(nameCell, shareCell, pinCell, actions);
      body.append(row);
    }
    if (!visible.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.textContent =
        "В потоке нет видимых офферов. Добавьте новый или покажите архив.";
      cell.className = "list-empty";
      row.append(cell);
      body.append(row);
    }
    const changed = streamDirty(stream);
    byId("offers-count").textContent = `Активных: ${active.length}`;
    byId("draft-total").textContent = `${numberFormat.format(total)}%`;
    byId("save-offers-button").disabled =
      !changed || !active.length || state.busy;
    byId("reset-offers-button").disabled = !changed || state.busy;
    byId("add-offer-button").disabled = state.busy;
    byId("editor-offer").disabled = state.busy;
    byId("show-archive").disabled = state.busy;
    byId("save-hint").textContent = !active.length
      ? "Перед публикацией добавьте или верните хотя бы один оффер."
      : changed
        ? "Черновик сохранён в базе. Push применит изменения в Keitaro, Cancel отменит их."
        : "Поток опубликован. Изменения сначала сохраняются в черновике.";
  }
  async function updateCampaign(path, body, successText) {
    if (state.busy || !state.campaign) return false;
    message("editor-error");
    byId("editor-success").hidden = true;
    state.busy = true;
    byId("editor-content").setAttribute("aria-busy", "true");
    for (const control of byId("editor-content").querySelectorAll(
      "button,input",
    ))
      control.disabled = true;
    byId("save-hint").textContent = "Сохраняем изменения…";
    try {
      const campaign = await request(path, {
        method: "POST",
        body: JSON.stringify({ revision: state.campaign.revision, ...body }),
      });
      state.campaign = campaign;
      if (!activeStream())
        state.streamId =
          campaign.streams.find((stream) => !stream.read_only)?.id ??
          campaign.streams[0]?.id ??
          null;
      if (successText) message("editor-success", successText);
      return true;
    } catch (error) {
      message(
        "editor-error",
        errorText(error) +
          (error.status === 409
            ? " Обновите кампанию, выбрав её в списке слева, и повторите действие."
            : ""),
      );
      return false;
    } finally {
      state.busy = false;
      byId("editor-content").removeAttribute("aria-busy");
      renderCampaign();
    }
  }
  async function mutateDraft(action, offerId, share) {
    if (state.busy || !activeStream() || activeStream().read_only) return false;
    return updateCampaign(
      `/api/campaigns/${state.campaign.id}/streams/${state.streamId}/draft`,
      { action, offer_id: offerId, ...(share !== undefined ? { share } : {}) },
    );
  }
  byId("create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy || !state.catalog) return;
    message("create-error");
    const offer = resolveOffer(byId("create-offer").value);
    const country = countryCode(byId("country-code").value);
    if (!country) {
      message(
        "create-error",
        "Выберите страну из списка или введите действующий двухбуквенный GEO-код, например AU.",
      );
      byId("country-code").focus();
      return;
    }
    if (!offer) {
      message(
        "create-error",
        "Выберите существующий оффер из списка или введите его точный ID.",
      );
      byId("create-offer").focus();
      return;
    }
    const name = byId("campaign-name").value.trim();
    if (!name) {
      message("create-error", "Введите название кампании.");
      byId("campaign-name").focus();
      return;
    }
    state.busy = true;
    byId("create-fields").disabled = true;
    byId("create-button").textContent = "Создаём…";
    byId("create-success").hidden = true;
    try {
      const data = await request("/api/campaigns", {
        method: "POST",
        body: JSON.stringify({
          name,
          country_code: country,
          offer_id: Number(offer.id),
          domain_id: Number(byId("domain-id").value),
          group_id: Number(byId("group-id").value),
          traffic_source_id: Number(byId("traffic-source-id").value),
        }),
      });
      const campaign = data.campaign || data;
      state.createdId = campaign.id;
      byId("created-campaign-name").textContent =
        `${campaign.name} · #${campaign.id}`;
      setSafeLink("created-campaign-url", campaign.url);
      byId("create-success").hidden = false;
      byId("campaign-search").value = "";
      await loadCampaigns();
    } catch (error) {
      message("create-error", errorText(error));
    } finally {
      state.busy = false;
      byId("create-fields").disabled = false;
      byId("create-button").textContent = "+ Создать кампанию";
    }
  });
  byId("add-offer-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy || !state.catalog) return;
    const offer = resolveOffer(byId("editor-offer").value);
    if (!offer) {
      message(
        "editor-error",
        "Выберите существующий оффер из списка или введите его точный ID.",
      );
      return;
    }
    if (
      (activeStream()?.offers || []).some(
        (item) => Number(item.offer_id) === Number(offer.id),
      )
    ) {
      message(
        "editor-error",
        "Этот оффер уже есть в потоке или архиве. Для архивного оффера нажмите Bring back.",
      );
      return;
    }
    if (await mutateDraft("add", Number(offer.id)))
      byId("editor-offer").value = "";
  });
  byId("save-offers-button").addEventListener("click", () => {
    if (streamDirty(activeStream()))
      updateCampaign(
        `/api/campaigns/${state.campaign.id}/streams/${state.streamId}/publish`,
        {},
        state.settings.mode === "live"
          ? "Поток опубликован в Keitaro."
          : "Поток опубликован в демонстрационном трекере.",
      );
  });
  byId("reset-offers-button").addEventListener("click", () => {
    if (streamDirty(activeStream()))
      updateCampaign(
        `/api/campaigns/${state.campaign.id}/streams/${state.streamId}/cancel`,
        {},
        "Черновик отменён. Восстановлено последнее опубликованное состояние.",
      );
  });
  byId("fetch-streams-button").addEventListener("click", () => {
    if (state.busy || !state.campaign) return;
    const discard = campaignDirty();
    if (
      discard &&
      !window.confirm(
        "Fetch streams заменит неопубликованные черновики текущим состоянием Keitaro. Продолжить?",
      )
    )
      return;
    updateCampaign(
      `/api/campaigns/${state.campaign.id}/fetch`,
      { discard_draft: discard },
      "Потоки обновлены. Удалённые из Keitaro офферы сохранены в архиве.",
    );
  });
  byId("show-archive").addEventListener("change", renderOffers);
  byId("tab-create").addEventListener("click", () => setTab("create"));
  byId("tab-editor").addEventListener("click", () => setTab("editor"));
  byId("empty-create-button").addEventListener("click", () => setTab("create"));
  byId("edit-created-button").addEventListener("click", () => {
    setTab("editor");
    loadCampaign(state.createdId);
  });
  byId("refresh-campaigns").addEventListener("click", loadCampaigns);
  byId("retry-button").addEventListener("click", initialize);
  byId("campaign-search").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(loadCampaigns, 250);
  });
  byId("country-code").addEventListener("input", () => {
    byId("route-country").textContent =
      byId("country-code").value.trim() || "Выбранный GEO";
  });
  byId("create-offer").addEventListener("input", () => {
    byId("route-offer").textContent =
      byId("create-offer").value.trim() || "Выбранный оффер";
  });
  initialize();
})();
