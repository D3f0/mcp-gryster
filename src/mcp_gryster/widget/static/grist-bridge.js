(() => {
  "use strict";

  const prefix = location.pathname.split("/widget/")[0];
  const state = {
    ready: false,
    fixture: null,
    records: [],
    callbacks: { onOptions: [], onRecords: [], onRecord: [] },
    failed: false,
    failureReported: false,
  };
  let reportQueue = Promise.resolve();

  const report = (payload) => {
    reportQueue = reportQueue.then(() => fetch(`${prefix}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      keepalive: true,
    })).then(() => undefined).catch(() => undefined);
    return reportQueue;
  };

  const locationOf = (source, line, column) => {
    let name = "unknown";
    if (typeof source === "string") {
      const parsed = source.split("/").pop();
      if (parsed && /^[A-Za-z0-9._-]+$/.test(parsed)) name = parsed;
    }
    const safeLine = Number.isInteger(line) && line > 0 ? line : 0;
    const safeColumn = Number.isInteger(column) && column > 0 ? column : 0;
    return `${name}:${safeLine}:${safeColumn}`;
  };

  window.addEventListener("error", (event) => {
    state.failed = true;
    state.failureReported = true;
    report({
      kind: "uncaught",
      category: "runtime",
      errorClass: event.error?.name || "Error",
      location: locationOf(event.filename, event.lineno, event.colno),
    }).finally(() => {
      document.body.dataset.mcpGrysterError = "runtime";
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    state.failed = true;
    state.failureReported = true;
    report({
      kind: "uncaught",
      category: "promise",
      errorClass: event.reason?.name || "Error",
      location: "unknown:0:0",
    }).finally(() => {
      document.body.dataset.mcpGrysterError = "promise";
    });
  });

  const unsupported = (method) => {
    state.failed = true;
    report({ kind: "unsupported", method }).finally(() => {
      document.body.dataset.mcpGrysterUnsupported = method;
    });
    const error = new Error("This Grist API member is unavailable in preview");
    error.name = "GristPreviewUnsupportedError";
    throw error;
  };

  const mappedRecord = (record) => {
    if (!record) return null;
    const mapped = { id: record.id };
    for (const [name, column] of Object.entries(state.fixture.mappings || {})) {
      mapped[name] = Array.isArray(column)
        ? column.map((item) => record.fields[item])
        : record.fields[column];
    }
    return mapped;
  };

  const findRecord = (id) => state.records.find((record) => record.id === id);

  const table = {
    async getRecords() {
      return structuredClone(state.records);
    },
    async getRecord(id) {
      const record = findRecord(id);
      return record ? structuredClone(record) : null;
    },
    async update(record) {
      if (state.fixture.accessLevel !== "full") return unsupported("getTable.update");
      const current = findRecord(record?.id);
      if (!current || !record.fields || typeof record.fields !== "object") {
        throw new TypeError("Invalid record update");
      }
      Object.assign(current.fields, structuredClone(record.fields));
      return structuredClone(current);
    },
    async create(record) {
      if (state.fixture.accessLevel !== "full") return unsupported("getTable.create");
      const next = structuredClone(record);
      state.records.push(next);
      return structuredClone(next);
    },
    async destroy(id) {
      if (state.fixture.accessLevel !== "full") return unsupported("getTable.destroy");
      const index = state.records.findIndex((record) => record.id === id);
      if (index >= 0) state.records.splice(index, 1);
    },
  };

  const register = (method, callback) => {
    if (typeof callback !== "function") throw new TypeError("Callback must be callable");
    state.callbacks[method].push(callback);
    report({ kind: "callback", method });
  };

  const dispatch = async () => {
    const selected = findRecord(state.fixture.selectedRecordId);
    await report({ kind: "event", method: "options" });
    for (const callback of state.callbacks.onOptions) {
      await callback(structuredClone(state.fixture.options), {});
    }
    await report({ kind: "event", method: "records" });
    for (const callback of state.callbacks.onRecords) {
      await callback(structuredClone(state.records), state.fixture.mappings);
    }
    await report({ kind: "event", method: "selectedRecord" });
    for (const callback of state.callbacks.onRecord) {
      await callback(mappedRecord(selected), state.fixture.mappings);
    }
    await reportQueue;
    if (!state.failed) {
      await report({ kind: "handshake" });
      document.body.dataset.mcpGrysterHandshake = "complete";
    }
  };

  const api = {
    onOptions(callback) { register("onOptions", callback); },
    onRecords(callback) { register("onRecords", callback); },
    onRecord(callback) { register("onRecord", callback); },
    mapColumnNames(record) { return mappedRecord(record); },
    getTable() {
      if (state.fixture?.accessLevel === "none") return unsupported("getTable");
      return table;
    },
    async ready() {
      if (state.ready) return;
      state.ready = true;
      await report({ kind: "ready" });
      try {
        const response = await fetch(`${prefix}/fixture`, { cache: "no-store" });
        if (!response.ok) throw new Error("Fixture unavailable");
        state.fixture = await response.json();
        state.records = structuredClone(state.fixture.records);
        await dispatch();
      } catch (error) {
        state.failed = true;
        if (!state.failureReported) {
          state.failureReported = true;
          await report({
            kind: "uncaught",
            category: "promise",
            errorClass: error?.name || "Error",
            location: "grist-bridge.js:0:0",
          });
          document.body.dataset.mcpGrysterError = "promise";
        }
      }
    },
  };

  window.grist = new Proxy(api, {
    get(target, member) {
      if (typeof member !== "string" || Reflect.has(target, member)) {
        return Reflect.get(target, member);
      }
      return () => unsupported(member);
    },
  });
})();
