"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function writeJsonAtomic(file, value) {
  ensureDir(path.dirname(file));
  const temp = `${file}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(value, null, 2), "utf8");
  fs.renameSync(temp, file);
}

function sha256(file) {
  const hash = crypto.createHash("sha256");
  hash.update(fs.readFileSync(file));
  return hash.digest("hex");
}

function safeNamePart(value) {
  return String(value || "")
    .normalize("NFKC")
    .replace(/[\\/:*?"<>|]+/g, "_")
    .replace(/\s+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function uniquePath(dir, baseName, extension) {
  const cleanBase = safeNamePart(baseName) || "epos_statement";
  const cleanExt = extension.startsWith(".") ? extension : `.${extension}`;
  let candidate = path.join(dir, `${cleanBase}${cleanExt}`);
  let index = 2;
  while (fs.existsSync(candidate)) {
    candidate = path.join(dir, `${cleanBase}_${index}${cleanExt}`);
    index += 1;
  }
  return candidate;
}

function isPathInside(parentDir, targetPath) {
  if (!parentDir || !targetPath) {
    return false;
  }
  const parent = path.resolve(parentDir);
  const target = path.resolve(targetPath);
  const relative = path.relative(parent, target);
  return relative === "" || (
    relative &&
    !relative.startsWith("..") &&
    !path.isAbsolute(relative)
  );
}

class HistoryStore {
  constructor(dataDir, outputDir = null) {
    this.file = path.join(dataDir, "history.json");
    this.outputDir = outputDir ? path.resolve(outputDir) : null;
    this.data = readJson(this.file, { version: 1, items: [] });
    if (!Array.isArray(this.data.items)) {
      this.data.items = [];
    }
  }

  list() {
    return [...this.data.items].sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)));
  }

  listExisting() {
    return this.list().filter((item) => (
      item.filePath &&
      (!this.outputDir || isPathInside(this.outputDir, item.filePath)) &&
      fs.existsSync(item.filePath)
    ));
  }

  listChecks() {
    return this.list().filter((item) => item.status === "not_issued");
  }

  find(yearMonth, format, service = "epos") {
    return this.data.items.find((item) => (
      (item.service || "epos") === service &&
      item.yearMonth === yearMonth &&
      item.format === format
    )) || null;
  }

  hasExistingFile(yearMonth, format, service = "epos") {
    const records = this.data.items.filter((item) => (
      (item.service || "epos") === service &&
      item.yearMonth === yearMonth &&
      item.format === format &&
      item.filePath
    ));
    for (const record of records) {
      if (this.outputDir && !isPathInside(this.outputDir, record.filePath)) {
        continue;
      }
      if (fs.existsSync(record.filePath)) {
        return record;
      }
    }
    return null;
  }

  add(record) {
    const item = {
      id: crypto.randomUUID(),
      createdAt: new Date().toISOString(),
      ...record
    };
    this.data.items.unshift(item);
    this.save();
    return item;
  }

  markNotIssued(record) {
    const service = record.service || "epos";
    const format = record.format || "pdf";
    this.data.items = this.data.items.filter((item) => !(
      item.status === "not_issued" &&
      (item.service || "epos") === service &&
      item.yearMonth === record.yearMonth &&
      (item.format || "pdf") === format
    ));
    return this.add({
      ...record,
      service,
      format,
      status: "not_issued"
    });
  }

  clear() {
    const count = this.data.items.length;
    this.data.items = [];
    this.save();
    return count;
  }

  save() {
    writeJsonAtomic(this.file, this.data);
  }
}

module.exports = {
  HistoryStore,
  ensureDir,
  isPathInside,
  safeNamePart,
  sha256,
  uniquePath,
  writeJsonAtomic
};

