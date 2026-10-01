/**
 * Corti memory plugin — client half (browser).
 *
 * Hand-written closure-factory bundle in the exact format the dsh web module
 * loader consumes (`window.__ModuleLoader__.load({ id, factory })`): this
 * file is both source and artifact, so no bundler step exists. It requires
 * only baseline module-table entries (react, ui-primitives) — no build-time
 * `@deepseek-ai/*` value imports, no CSS pipeline (inline styles over the
 * shared `--dsw-*` tokens).
 *
 * Registers one `settings.general.item` row: an on/off Switch for the
 * volatile `enabled` field of the `corti-memory` settings namespace (dsh
 * 0.1.7 settings model — the namespace is the Loader entry id, i.e. this
 * package). Reads and writes go through `remote.settings` with
 * revision-checked (CAS) updates; a write lands as a profile-layer patch
 * override the host re-reads live from its volatile Config ref — no restart
 * needed, and the host gates recall injection, capture, and the memory
 * tools on the same value.
 */
window.__ModuleLoader__.load({ id: "corti-memory", factory: (require) => {
  const React = require("react");
  const { Switch } = require("@deepseek-ai/dsh-client-ui-primitives");

  /** Locale namespace (also the slot registration's `locale` key). */
  const NS = "corti-memory";
  /** Settings namespace = Loader entry id of the host half's plugin row. */
  const SETTINGS_NAMESPACE = "corti-memory";

  const en = {
    "title": "Corti memory",
    "description": "Persistent cross-session memory: recall injection, capture, and memory tools. Turning this off stops injection and capture immediately.",
    "error": "Save failed: {{message}}",
    "unavailable": "Host plugin does not expose the memory switch (update corti-memory).",
  };

  // Unicode escapes keep this file ASCII (repo check-cjk policy).
  const zh = {
    "title": "Corti \u8bb0\u5fc6",
    "description": "\u8de8\u4f1a\u8bdd\u6301\u4e45\u8bb0\u5fc6\uff1a\u53ec\u56de\u6ce8\u5165\u3001\u81ea\u52a8\u6355\u83b7\u4e0e\u8bb0\u5fc6\u5de5\u5177\u3002\u5173\u95ed\u540e\u7acb\u5373\u505c\u6b62\u6ce8\u5165\u4e0e\u6355\u83b7\u3002",
    "error": "\u4fdd\u5b58\u5931\u8d25\uff1a{{message}}",
    "unavailable": "\u5bbf\u4e3b\u63d2\u4ef6\u672a\u66b4\u9732\u8bb0\u5fc6\u5f00\u5173\uff08\u8bf7\u66f4\u65b0 corti-memory\uff09\u3002",
  };

  // Row geometry mirrors the native DeveloperToolsRow (Settings/General):
  // label block left, Switch right, hairline separator under the row.
  const rowStyle = {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "24px",
    padding: "16px 0",
    borderBottom: "0.5px solid var(--dsw-alias-border-l2)",
  };
  const titleStyle = { fontSize: "14px", lineHeight: "20px" };
  const descriptionStyle = {
    marginTop: "4px",
    color: "var(--dsw-alias-label-secondary)",
    fontSize: "12px",
    lineHeight: "18px",
  };
  const alertStyle = {
    marginTop: "4px",
    color: "var(--dsw-alias-danger, #c0392b)",
    fontSize: "12px",
  };

  /**
   * The General settings row. Data rides two narrow inject functions
   * (`loadConfig` / `save`) so the component stays free of dsh remote types;
   * copy arrives through the locale seat `t`.
   */
  function CortiMemoryRow({ loadConfig, save, t }) {
    // undefined = loading, null = namespace absent (host half too old), else the value.
    const [loaded, setLoaded] = React.useState(undefined);
    const [error, setError] = React.useState(null);
    const [busy, setBusy] = React.useState(false);

    React.useEffect(() => {
      let alive = true;
      loadConfig().then((config) => {
        if (alive) setLoaded(config);
      }, (err) => {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      });
      return () => { alive = false; };
    }, [loadConfig]);

    const unavailable = loaded === null;
    const enabled = unavailable || loaded === undefined ? true : loaded.enabled === true;
    const disabled = busy || loaded === undefined || unavailable;

    const onChange = (next) => {
      if (disabled) return;
      setBusy(true);
      setError(null);
      save({ enabled: next }, loaded.revision).then((revision) => {
        setLoaded({ enabled: next, revision });
      }, (err) => {
        setError(err instanceof Error ? err.message : String(err));
      }).finally(() => { setBusy(false); });
    };

    return React.createElement("div", { style: rowStyle },
      React.createElement("div", null,
        React.createElement("div", { style: titleStyle }, t("title")),
        React.createElement("div", { style: descriptionStyle },
          unavailable ? t("unavailable") : t("description")),
        error !== null
          ? React.createElement("div", { role: "alert", style: alertStyle }, t("error", { message: error }))
          : null),
      React.createElement(Switch, {
        checked: enabled,
        disabled: disabled,
        label: t("title"),
        title: unavailable ? t("unavailable") : undefined,
        onChange: onChange,
      }));
  }

  /** Required client services (cordis fiber inject). */
  const inject = ["slots", "locale", "remote", "remote.settings"];

  /** Register the locale dictionaries and the General settings row. */
  function apply(ctx) {
    ctx.effect(() => ctx.locale.register(NS, { zh, en }), "corti-memory: dictionaries");

    ctx.slots.inject("settings.general.item", function* () {
      yield ctx.slots.register({
        name: "settings.general.item",
        id: "corti-memory",
        order: 120,
        locale: NS,
        inject: () => ({
          loadConfig: async () => {
            const response = await ctx.remote.settings.describe();
            if (!response.ok) throw new Error(`${response.error.code}: ${response.error.message}`);
            const view = response.value.namespaces.find((entry) => entry.ns === SETTINGS_NAMESPACE);
            if (view === undefined) return null;
            const value = view.value;
            return {
              enabled: value !== null && typeof value === "object" && typeof value.enabled === "boolean"
                ? value.enabled
                : true,
              revision: view.revision,
            };
          },
          save: async (patch, revision) => {
            const response = await ctx.remote.settings.update(SETTINGS_NAMESPACE, patch, revision);
            if (!response.ok) throw new Error(`${response.error.code}: ${response.error.message}`);
            return response.value.revision;
          },
        }),
      }, CortiMemoryRow);
    });
  }

  const module = { exports: {} };
  module.exports = { inject, apply };
  return module.exports;
} });
