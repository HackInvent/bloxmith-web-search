/** Package-owned Apply: provider-dependent fields, typed values and exact settings replacement through the public action API. */
export function bindSettings(root, api) {
  const controller = new AbortController();
  const button = root.querySelector("[data-owned-apply]");
  if (!button) return () => controller.abort();
  let dirty = false, pending = false, generation = 0;
  const t = (key, fallback) => api.t?.(`block.web_search.${key}`, {}, fallback) || fallback;
  const current = name => {
    const field = root.querySelector(`[data-block-config-field="${name}"]`);
    if (!field) return undefined;
    return field.type === "checkbox" ? field.checked : field.value;
  };
  const syncConditional = () => {
    for (const section of root.querySelectorAll("[data-show-field]")) {
      let expected;
      try { expected = JSON.parse(section.dataset.showValue); } catch { expected = section.dataset.showValue; }
      const value = current(section.dataset.showField);
      section.hidden = Array.isArray(expected) ? !expected.includes(value) : value !== expected;
    }
  };
  syncConditional();
  const status = document.createElement("p");
  status.className = "field-hint owned-form-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  button.parentElement.before(status);
  const sync = () => { button.disabled = !dirty || pending || Boolean(api.isReadOnly?.()); };
  const mark = event => {
    if (!event.target.matches("input,textarea,select")) return;
    generation += 1; dirty = true; status.textContent = ""; syncConditional(); sync();
  };
  const value = field => {
    if (field.type === "checkbox") return field.checked;
    const type = field.dataset.blockValueType;
    if (type === "integer" || field.type === "number") {
      const parsed = Number(field.value);
      if (!field.value.trim() || !Number.isFinite(parsed) || !Number.isInteger(parsed) || !field.checkValidity()) {
        const section = field.closest(".owned-advanced");
        if (section) section.open = true;
        field.reportValidity(); field.focus();
        throw new Error(t("invalid_number", "Check the numeric value and its limits."));
      }
      return parsed;
    }
    return field.value;
  };
  const apply = async () => {
    if (!dirty || pending || api.isReadOnly?.()) return;
    const started = generation;
    try {
      const patch = {config: {}};
      for (const field of root.querySelectorAll("[data-block-title-field],[data-block-description-field],[data-block-config-field]")) {
        const current = value(field);
        if (field.dataset.blockConfigField) patch.config[field.dataset.blockConfigField] = current;
        if (field.hasAttribute("data-block-title-field")) patch.title = current;
        if (field.hasAttribute("data-block-description-field")) patch.description = current;
      }
      pending = true; sync(); status.textContent = "";
      const result = await api.applyAction("save_settings", {node_patch: patch});
      if (result?.error) throw new Error(result.error);
      if (controller.signal.aborted) return;
      if (started === generation) dirty = false;
      status.textContent = t("saved", "Settings applied.");
    } catch (error) {
      if (!controller.signal.aborted) status.textContent = String(error.message || error);
    } finally {
      pending = false;
      if (!controller.signal.aborted) sync();
    }
  };
  root.addEventListener("input", mark, {signal: controller.signal});
  root.addEventListener("change", mark, {signal: controller.signal});
  button.addEventListener("click", event => {event.preventDefault(); void apply();}, {signal: controller.signal});
  root.addEventListener("keydown", event => {
    if (event.key === "Enter" && event.target instanceof HTMLInputElement && event.target.type !== "checkbox") {
      event.preventDefault(); void apply();
    }
  }, {signal: controller.signal});
  sync();
  return () => {controller.abort(); status.remove();};
}
