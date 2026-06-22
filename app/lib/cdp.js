"use strict";

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url, options = {}, timeoutMs = 5000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

class CDPConnection {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.ws = null;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.closed = false;
  }

  static connect(wsUrl, timeoutMs = 10000) {
    const connection = new CDPConnection(wsUrl);
    return connection.open(timeoutMs);
  }

  open(timeoutMs) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error("Timed out connecting to browser debugger."));
      }, timeoutMs);

      const ws = new WebSocket(this.wsUrl);
      this.ws = ws;

      ws.addEventListener("open", () => {
        clearTimeout(timer);
        resolve(this);
      });

      ws.addEventListener("error", (event) => {
        clearTimeout(timer);
        reject(new Error(event.message || "Browser debugger connection failed."));
      }, { once: true });

      ws.addEventListener("message", (event) => {
        this.handleMessage(event.data);
      });

      ws.addEventListener("close", () => {
        this.closed = true;
        for (const { reject: rejectPending } of this.pending.values()) {
          rejectPending(new Error("Browser debugger connection closed."));
        }
        this.pending.clear();
      });
    });
  }

  on(method, handler) {
    if (!this.listeners.has(method)) {
      this.listeners.set(method, new Set());
    }
    this.listeners.get(method).add(handler);
    return () => this.listeners.get(method)?.delete(handler);
  }

  emit(method, params, sessionId) {
    const handlers = this.listeners.get(method);
    if (!handlers) {
      return;
    }
    for (const handler of handlers) {
      try {
        handler(params || {}, sessionId || null);
      } catch (error) {
        console.error("CDP event handler failed:", error.message);
      }
    }
  }

  handleMessage(raw) {
    let text = raw;
    if (raw instanceof ArrayBuffer) {
      text = Buffer.from(raw).toString("utf8");
    } else if (ArrayBuffer.isView(raw)) {
      text = Buffer.from(raw.buffer, raw.byteOffset, raw.byteLength).toString("utf8");
    }

    let message;
    try {
      message = JSON.parse(String(text));
    } catch {
      return;
    }

    if (message.id && this.pending.has(message.id)) {
      const pending = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) {
        pending.reject(new Error(message.error.message || JSON.stringify(message.error)));
      } else {
        pending.resolve(message.result || {});
      }
      return;
    }

    if (message.method) {
      this.emit(message.method, message.params, message.sessionId);
    }
  }

  send(method, params = {}, sessionId = null) {
    if (!this.ws || this.closed || this.ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("Browser debugger is not connected."));
    }

    const id = this.nextId++;
    const message = { id, method, params };
    if (sessionId) {
      message.sessionId = sessionId;
    }

    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(message));
    });
  }

  waitForEvent(method, predicate = () => true, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        off();
        reject(new Error(`Timed out waiting for ${method}.`));
      }, timeoutMs);

      const off = this.on(method, (params, sessionId) => {
        if (!predicate(params, sessionId)) {
          return;
        }
        clearTimeout(timer);
        off();
        resolve({ params, sessionId });
      });
    });
  }

  close() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.close();
    }
  }
}

async function waitForDebugPort(port, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < end) {
    try {
      return await fetchJson(`http://127.0.0.1:${port}/json/version`, {}, 1500);
    } catch (error) {
      lastError = error;
      await delay(250);
    }
  }
  throw new Error(`Could not connect to Edge debugging port ${port}: ${lastError?.message || "timeout"}`);
}

module.exports = {
  CDPConnection,
  delay,
  fetchJson,
  waitForDebugPort
};

