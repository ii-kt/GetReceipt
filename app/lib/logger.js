"use strict";

const fs = require("fs");
const path = require("path");

class AppLogger {
  constructor(logDir, options = {}) {
    this.logDir = logDir;
    this.maxMemory = options.maxMemory || 300;
    this.memory = [];
    this.onLine = options.onLine || null;
    fs.mkdirSync(this.logDir, { recursive: true });
    this.logFile = path.join(this.logDir, "app.log");
  }

  entries() {
    return [...this.memory];
  }

  info(message, details) {
    this.write("info", message, details);
  }

  warn(message, details) {
    this.write("warn", message, details);
  }

  error(message, details) {
    this.write("error", message, details);
  }

  write(level, message, details) {
    const entry = {
      at: new Date().toISOString(),
      level,
      message: String(message || ""),
      details: details || null
    };

    this.memory.push(entry);
    if (this.memory.length > this.maxMemory) {
      this.memory.splice(0, this.memory.length - this.maxMemory);
    }

    const line = JSON.stringify(entry) + "\n";
    fs.appendFile(this.logFile, line, (error) => {
      if (error) {
        console.error("Failed to write log:", error.message);
      }
    });

    if (this.onLine) {
      this.onLine(entry);
    }
  }
}

module.exports = { AppLogger };

