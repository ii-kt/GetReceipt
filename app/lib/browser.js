Warning: truncated output (original token count: 5274)
Total output lines: 666

"use strict";

const fs = require("fs");
const net = require("net");
const path = require("path");
const { spawn, spawnSync } = require("child_process");
const { CDPConnection, delay, waitForDebugPort } = require("./cdp");
const { ensureDir } = require("./store");

function fileExists(file) {
  try {
    return fs.existsSync(file);
  } catch {
    return false;
  }
}

function findEdgeExecutable() {
  const candidates = [
    path.join(process.env["ProgramFiles(x86)"] || "", "Microsoft", "Edge", "Application", "msedge.exe"),
    path.join(process.env.ProgramFiles || "", "Microsoft", "Edge", "Application", "msedge.exe"),
    path.join(process.env.LOCALAPPDATA || "", "Microsoft", "Edge", "Application", "msedge.exe")
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (fileExists(candidate)) {
      return candidate;
    }
  }

  const where = spawnSync("where.exe", ["msedge.exe"], { encoding: "utf8" });
  if (where.status === 0) {
    const first = where.stdout.split(/\r?\n/).map((line) => line.trim()).find(Boolean);
    if (first && fileExists(first)) {
      return first;
    }
  }

  return null;
}

function findExecutable(command, candidates) {
  for (const candidate of candidates.filter(Boolean)) {
    if (fileExists(candidate)) {
      return candidate;
    }
  }

  const locator = process.platform === "win32" ? "where.exe" : "which";
  const located = spawnSync(locator, [command], { encoding: "utf8" });
  if (located.status === 0) {
    const first = located.stdout.split(/\r?\n/).map((line) => line.trim()).find(Boolean);
    if (first && fileExists(first)) {
      return first;
    }
  }

  return null;
}

function findEnvBrowserExecutable() {
  return [
    process.env.BROWSER_EXECUTABLE,
    process.env.CHROME_BIN,
    process.env.CHROMIUM_BIN,
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
  ]
    .map((candidate) => candidate && path.resolve(candidate))
    .find(fileExists) || null;
}

function findChromeExecutable() {
  const command = process.platform === "win32" ? "chrome.exe" : "google-chrome";
  return findExecutable(command, [
    path.join(process.env.ProgramFiles || "", "Google", "Chrome", "Application", "chrome.exe"),
    path.join(process.env["ProgramFiles(x86)"] || "", "Google", "Chrome", "Application", "chrome.exe"),
    path.join(process.env.LOCALAPPDATA || "", "Google", "Chrome", "Application", "chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome"
  ]);
}

function findChromiumExecutable() {
  const command = process.platform === "win32" ? "chromium.exe" : "chromium";
  return findExecutable(command, [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium"
  ]);
}

function findBraveExecutable() {
  return findExecutable("brave.exe", [
    path.join(process.env.ProgramFiles || "", "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
    path.join(process.env["ProgramFiles(x86)"] || "", "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
    path.join(process.env.LOCALAPPDATA || "", "BraveSoftware", "Brave-Browser", "Application", "brave.exe")
  ]);
}

function findVivaldiExecutable() {
  return findExecutable("vivaldi.exe", [
    path.join(process.env.LOCALAPPDATA || "", "Vivaldi", "Application", "vivaldi.exe"),
    path.join(process.env.ProgramFiles || "", "Vivaldi", "Application", "vivaldi.exe"),
    path.join(process.env["ProgramFiles(x86)"] || "", "Vivaldi", "Application", "vivaldi.exe")
  ]);
}

function findDefaultBrowserProgId() {
  for (const protocol of ["https", "http"]) {
    const reg = spawnSync("reg.exe", [
      "query",
      `HKCU\\Software\\Microsoft\\Windows\\Shell\\Associations\\UrlAssociations\\${protocol}\\UserChoice`,
      "/v",
      "ProgId"
    ], { encoding: "utf8" });
    if (reg.status !== 0) {
      continue;
    }
    const line = reg.stdout.split(/\r?\n/).find((entry) => /\bProgId\b/i.test(entry));
    const match = line && line.match(/\bREG_…3274 tokens truncated…ispatchKeyEvent", { type: "rawKeyDown", ...params }, sessionId);
    await this.connection.send("Input.dispatchKeyEvent", { type: "keyUp", ...params }, sessionId);
  }

  async insertText(text) {
    const { sessionId } = await this.ensurePage();
    await this.connection.send("Input.insertText", { text: String(text || "") }, sessionId);
  }

  async captureScreenshot() {
    const { sessionId } = await this.ensurePage();
    const result = await this.connection.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: false
    }, sessionId);
    return Buffer.from(result.data || "", "base64");
  }

  async printToPdf(targetFile, options = {}) {
    const { sessionId } = await this.ensurePage();
    const result = await this.connection.send("Page.printToPDF", {
      printBackground: true,
      preferCSSPageSize: true,
      marginTop: 0.4,
      marginBottom: 0.4,
      marginLeft: 0.35,
      marginRight: 0.35,
      ...options
    }, sessionId);
    fs.writeFileSync(targetFile, Buffer.from(result.data || "", "base64"));
    return targetFile;
  }

  async pageSummary() {
    return await this.evaluate(`(() => {
      const body = document.body;
      const text = body ? body.innerText.replace(/\\s+/g, " ").slice(0, 8000) : "";
      const passwordFields = document.querySelectorAll("input[type='password']").length;
      const visibleInputs = [...document.querySelectorAll("input, select, button, a")]
        .filter((el) => {
          const rect = el.getBoundingClientRect();
          const style = getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
        })
        .slice(0, 80)
        .map((el) => ({
          tag: el.tagName.toLowerCase(),
          type: el.getAttribute("type") || "",
          text: (el.innerText || el.value || el.getAttribute("aria-label") || el.title || "").trim().slice(0, 80),
          href: el.href || ""
        }));
      return {
        url: location.href,
        title: document.title,
        text,
        passwordFields,
        visibleInputs
      };
    })()`);
  }

  async getTargets() {
    await this.ensureStarted();
    const targets = await this.connection.send("Target.getTargets");
    return targets.targetInfos || [];
  }

  async getCookiesFor(url) {
    const { sessionId } = await this.ensurePage();
    const result = await this.connection.send("Network.getCookies", { urls: [url] }, sessionId);
    return result.cookies || [];
  }

  clearDownloadDir() {
    clearDirectory(this.downloadDir);
  }

  async waitForDownload(format, markerMs, timeoutMs = 90000) {
    const extension = `.${format.toLowerCase()}`;
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      const candidates = listFiles(this.downloadDir)
        .filter((entry) => entry.stat.isFile())
        .filter((entry) => !entry.name.endsWith(".crdownload") && !entry.name.endsWith(".tmp"))
        .filter((entry) => entry.stat.mtimeMs >= markerMs - 1000)
        .sort((a, b) => b.stat.mtimeMs - a.stat.mtimeMs);

      const preferred = candidates.find((entry) => entry.name.toLowerCase().endsWith(extension)) || candidates[0];
      if (preferred) {
        await waitForStableFile(preferred.path);
        return preferred.path;
      }

      const active = listFiles(this.downloadDir).some((entry) => entry.name.endsWith(".crdownload"));
      await delay(active ? 300 : 250);
    }
    return null;
  }

  async closeAutomationBrowser() {
    if (this.connection) {
      try {
        await this.connection.send("Browser.close");
      } catch {
        this.connection.close();
      }
    }
    this.connection = null;
    this.process = null;
    this.pageTargetId = null;
    this.pageSessionId = null;
  }
}

module.exports = {
  ManagedEdge,
  clearDirectory,
  findEdgeExecutable,
  findAutomationBrowserExecutable,
  findDefaultChromiumExecutable,
  findFreePort
};

