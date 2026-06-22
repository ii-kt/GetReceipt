Warning: truncated output (original token count: 5042)
Total output lines: 636

"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");
const { spawn } = require("child_process");
const { URL } = require("url");
const { EposAutomation, UserActionError } = require("./lib/eposAutomation");
const { CommufaAutomation } = require("./lib/commufaAutomation");
const { TokutenAutomation } = require("./lib/tokutenAutomation");
const { WebBillingAutomation } = require("./lib/webbillingAutomation");
const { AppLogger } = require("./lib/logger");
const { findFreePort } = require("./lib/browser");
const { HistoryStore, ensureDir } = require("./lib/store");

const ROOT_DIR = path.resolve(__dirname, "..");
const OUTPUT_DIR = path.join(ROOT_DIR, "_output");
const DATA_DIR = path.join(ROOT_DIR, "data");
const LOG_DIR = path.join(ROOT_DIR, "logs");
const PUBLIC_DIR = path.join(__dirname, "public");
const EPOS_CONFIG_FILE = path.join(__dirname, "config", "siteSelectors.json");
const COMMUFA_CONFIG_FILE = path.join(__dirname, "config", "commufaSelectors.json");
const TOKUTEN_CONFIG_FILE = path.join(__dirname, "config", "tokutenSelectors.json");
const WEBBILLING_CONFIG_FILE = path.join(__dirname, "config", "webbillingSelectors.json");
const TARGET_MONTH_START = { year: 2026, month: 1 };

ensureDir(OUTPUT_DIR);
ensureDir(DATA_DIR);
ensureDir(LOG_DIR);

const eposConfig = JSON.parse(fs.readFileSync(EPOS_CONFIG_FILE, "utf8"));
const commufaConfig = JSON.parse(fs.readFileSync(COMMUFA_CONFIG_FILE, "utf8"));
const tokutenConfig = JSON.parse(fs.readFileSync(TOKUTEN_CONFIG_FILE, "utf8"));
const webbillingConfig = JSON.parse(fs.readFileSync(WEBBILLING_CONFIG_FILE, "utf8"));
const logger = new AppLogger(LOG_DIR);
const history = new HistoryStore(DATA_DIR, OUTPUT_DIR);
const automations = {
  epos: new EposAutomation({
    rootDir: ROOT_DIR,
    outputDir: OUTPUT_DIR,
    dataDir: DATA_DIR,
    history,
    logger,
    config: eposConfig
  }),
  commufa: new CommufaAutomation({
    rootDir: ROOT_DIR,
    outputDir: OUTPUT_DIR,
    dataDir: DATA_DIR,
    history,
    logger,
    config: commufaConfig
  }),
  tokuten: new TokutenAutomation({
    rootDir: ROOT_DIR,
    outputDir: OUTPUT_DIR,
    dataDir: DATA_DIR,
    history,
    logger,
    config: tokutenConfig
  }),
  mobile: new WebBillingAutomation({
    rootDir: ROOT_DIR,
    outputDir: OUTPUT_DIR,
    dataDir: DATA_DIR,
    history,
    logger,
    config: webbillingConfig
  })
};

const services = {
  epos: {
    id: "epos",
    name: "EPOS",
    targetUrl: eposConfig.targetUrl
  },
  commufa: {
    id: "commufa",
    name: "繧ｳ繝溘Η繝輔ぃ",
    targetUrl: commufaConfig.targetUrl
  },
  tokuten: {
    id: "tokuten",
    name: "繝医け繝・Φ縺ｧ繧薙″",
    targetUrl: tokutenConfig.targetUrl
  },
  mobile: {
    id: "mobile",
    name: "謳ｺ蟶ｯ",
    targetUrl: webbillingConfig.targetUrl
  }
};

function serviceIdFromRequest(value) {
  const id = String(value || "epos").toLowerCase();
  if (automations[id]) {
    return id;
  }
  return "epos";
}

const taskState = {
  running: false,
  phase: "idle",
  service: "epos",
  message: "蠕・ｩ滉ｸｭ縺ｧ縺吶・,
  error: null,
  lastResult: null,
  targetYearMonth: null,
  requestedFormat: null,
  startedAt: null,
  finishedAt: null
};

let lastHeartbeat = Date.now();
let server = null;
let shuttingDown = false;

function setTaskState(patch) {
  Object.assign(taskState, patch);
}

function contentTypeFor(file) {
  const ext = path.extname(file).toLowerCase();
  return {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8"
  }[ext] || "application/octet-stream";
}

function sendJson(response, statusCode, value) {
  const body = JSON.stringify(value);
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body)
  });
  response.…3042 tokens truncated…|| "Enter");
      sendJson(response, 200, { ok: true });
    } catch (error) {
      sendJson(response, 500, { ok: false, error: errorPayload(error) });
    }
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/open-output") {
    spawn("explorer.exe", [OUTPUT_DIR], { detached: true, stdio: "ignore" }).unref();
    sendJson(response, 200, { ok: true });
    return;
  }

  if (request.method === "POST" && url.pathname === "/api/shutdown") {
    sendJson(response, 200, { ok: true });
    shutdownSoon();
    return;
  }

  sendJson(response, 404, { ok: false, error: { message: "API not found." } });
}

function serveStatic(request, response, url) {
  const rawPath = url.pathname === "/" ? "/index.html" : decodeURIComponent(url.pathname);
  const safePath = path.normalize(rawPath).replace(/^([/\\])+/, "");
  const file = path.join(PUBLIC_DIR, safePath);
  if (!file.startsWith(PUBLIC_DIR) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    sendText(response, 404, "Not found");
    return;
  }
  response.writeHead(200, { "Content-Type": contentTypeFor(file) });
  fs.createReadStream(file).pipe(response);
}

function openAppWindow(port) {
  if (process.env.GETRECEIPT_NO_OPEN === "1") {
    logger.info(`繧｢繝励Μ逕ｻ髱｢: http://127.0.0.1:${port}/`);
    return;
  }

  const url = `http://127.0.0.1:${port}/`;
  spawn("cmd.exe", ["/c", "start", "", url], { detached: true, stdio: "ignore" }).unref();
}

async function tryOpenExistingApp(port = 18765) {
  if (process.env.GETRECEIPT_NO_REUSE === "1") {
    return false;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 800);
  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/state`, { signal: controller.signal });
    if (!response.ok) {
      return false;
    }
    const state = await response.json();
    if (state.appName !== "譏守ｴｰ蜿門ｾ励い繝励Μ") {
      return false;
    }
    logger.info("譌｢縺ｫ襍ｷ蜍輔＠縺ｦ縺・ｋ繧｢繝励Μ逕ｻ髱｢繧帝幕縺阪∪縺吶・, { port });
    openAppWindow(port);
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function shutdownSoon() {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  logger.info("繧｢繝励Μ繧堤ｵゆｺ・＠縺ｾ縺吶・);
  setTimeout(async () => {
    for (const automation of Object.values(automations)) {
      try {
        await automation.shutdown();
      } catch {
        // Best effort shutdown.
      }
    }
    if (server) {
      server.close(() => process.exit(0));
      setTimeout(() => process.exit(0), 1500).unref();
    } else {
      process.exit(0);
    }
  }, 250);
}

async function main() {
  if (await tryOpenExistingApp(18765)) {
    return;
  }

  const port = await findFreePort(18765);
  server = http.createServer((request, response) => {
    const url = new URL(request.url, `http://${request.headers.host || "127.0.0.1"}`);
    if (url.pathname.startsWith("/api/")) {
      handleApi(request, response, url).catch((error) => {
        logger.error("API error", errorPayload(error));
        sendJson(response, 500, { ok: false, error: errorPayload(error) });
      });
      return;
    }
    serveStatic(request, response, url);
  });

  server.listen(port, "127.0.0.1", () => {
    logger.info("繧｢繝励Μ繧ｵ繝ｼ繝舌・繧定ｵｷ蜍輔＠縺ｾ縺励◆縲・, { port, outputDir: OUTPUT_DIR });
    openAppWindow(port);
  });

  setInterval(() => {
    const idleMs = Date.now() - lastHeartbeat;
    if (!taskState.running && idleMs > 120000 && process.env.GETRECEIPT_KEEP_ALIVE !== "1") {
      shutdownSoon();
    }
  }, 30000).unref();
}

process.on("SIGINT", shutdownSoon);
process.on("SIGTERM", shutdownSoon);

main().catch((error) => {
  logger.error("襍ｷ蜍輔↓螟ｱ謨励＠縺ｾ縺励◆縲・, errorPayload(error));
  console.error(error);
  process.exit(1);
});

