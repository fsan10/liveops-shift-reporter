(function (root) {
  "use strict";

  function contract(payload) {
    const settings = payload.settings || {};
    const secrets = new Set(payload.secret_keys || []);
    return { supported: new Set([...Object.keys(settings), ...secrets]), secrets };
  }

  function changedValues(current, saved, schema) {
    const values = {};
    const unsupported = [];
    for (const [key, value] of Object.entries(current)) {
      if (!schema.supported.has(key)) {
        if (value !== String(saved[key] ?? "")) unsupported.push(key);
        continue;
      }
      if (schema.secrets.has(key)) {
        // A masked public value can never stand in for a new credential.
        if (value.trim()) values[key] = value;
      } else if (value !== String(saved[key] ?? "")) {
        values[key] = value;
      }
    }
    return { values, unsupported };
  }

  root.liveopsSettingsForm = { contract, changedValues };
})(globalThis);
