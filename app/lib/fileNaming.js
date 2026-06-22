Warning: truncated output (original token count: 2200)
Total output lines: 261

"use strict";

const fs = require("fs");
const zlib = require("zlib");
const { safeNamePart, uniquePath } = require("./store");

class MetadataExtractionError extends Error {
  constructor(message, advice, code = "FILE_METADATA_NOT_FOUND") {
    super(message);
    this.name = "MetadataExtractionError";
    this.advice = advice;
    this.code = code;
  }
}

function normalizeText(value) {
  return String(value || "")
    .normalize("NFKC")
    .replace(/\u00a0/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function formatDate(year, month, day) {
  return `${String(year).padStart(4, "0")}${String(month).padStart(2, "0")}${String(day).padStart(2, "0")}`;
}

function isValidDate(year, month, day) {
  const date = new Date(Number(year), Number(month) - 1, Number(day));
  return date.getFullYear() === Number(year) &&
    date.getMonth() === Number(month) - 1 &&
    date.getDate() === Number(day);
}

function parseDateParts(year, month, day) {
  return isValidDate(year, month, day) ? formatDate(year, month, day) : null;
}

function contextAround(text, index, size = 80) {
  return text.slice(Math.max(0, index - size), Math.min(text.length, index + size));
}

function dateSourceForContext(context) {
  if (/(?:\u652f\u6255|\u632f\u66ff|\u5f15\u843d|\u53e3\u5ea7)/.test(context)) {
    return { source: "payment", score: 3 };
  }
  if (/(?:\u8acb\u6c42|\u767a\u884c|\u4f5c\u6210|\u5229\u7528)/.test(context)) {
    return { source: "issue", score: 2 };
  }
  return { source: "text", score: 1 };
}

function extractTransactionDate(text) {
  const normalized = normalizeText(text);
  const candidates = [];

  const separated = /((?:19|20)\d{2})\D{1,6}(\d{1,2})\D{1,6}(\d{1,2})/g;
  for (const match of normalized.matchAll(separated)) {
    const value = parseDateParts(match[1], match[2], match[3]);
    if (!value) continue;
    const source = dateSourceForContext(contextAround(normalized, match.index || 0));
    candidates.push({ value, index: match.index || 0, ...source });
  }

  const compact = /((?:19|20)\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])/g;
  for (const match of normalized.matchAll(compact)) {
    const value = parseDateParts(match[1], match[2], match[3]);
    if (!value) continue;
    const source = dateSourceForContext(contextAround(normalized, match.index || 0));
    candidates.push({ value, index: match.index || 0, ...source });
  }

  candidates.sort((a, b) => (b.score - a.score) || (a.index - b.index));
  return candidates[0] ? { value: candidates[0].value, source: candidates[0].source } : null;
}

function normalizeAmount(value) {
  return String(value || "")
    .normalize("NFKC")
    .replace(/[,\s\u5186\uffe5\u00a5\u8700\uff82\uff65'"]/g, "")
    .replace(/\.00$/, "");
}

function amountSourceForContext(context) {
  if (/(?:\u5408\u8a08|\u8acb\u6c42|\u652f\u6255|\u91d1\u984d|\u7a0e\u8fbc)/.test(context)) {
    return { source: "label", score: 3 };
  }
  if (/(?:\u5229\u7528|\u660e\u7d30)/.test(context)) {
    return { source: "detail", score: 2 };
  }
  return { source: "text", score: 1 };
}

function extractTransactionAmount(text) {
  const normalized = normalizeText(text);
  const candidates = [];
  const amountCore = "([0-9][0-9,\\s]{1,})";
  const currencyAfter = new RegExp(`${amountCore}\\s*(?:\\u5186|\\uffe5|\\u00a5|\\u8700|\\uff82\\uff65)`, "g");
  const currencyBefore = new RegExp(`(?:\\u5186|\\uffe5|\\u00a5|\\u8700|\\uff82\\uff65)\\s*${amountCore}`, "g");

  for (const pattern of [currencyAfter, currencyBefore]) {
    for (const match of normalized.matchAll(pattern)) {
      const value = normalizeAmount(match[1]);
      if (!/^\d+$/.test(value)) continue;
      const source = amountSourceForContext(contextAround(normalized, match.index || 0, 120));
      candidates.push({ value, amount: Number(value), index: match.index || 0, ...source });
    }
  }

  candidates.sort((a, b) => (b.score - a.score) || (b.amount - a.amount) || (a.index - b.index));
  return candidates[0] ? { value: candidates[0].value, source: candidat…200 tokens truncated…    if (char !== "\\") {
      bytes.push(char.charCodeAt(0) & 0xff);
      continue;
    }

    index += 1;
    const escaped = value[index];
    if (escaped === "n") bytes.push(10);
    else if (escaped === "r") bytes.push(13);
    else if (escaped === "t") bytes.push(9);
    else if (escaped === "b") bytes.push(8);
    else if (escaped === "f") bytes.push(12);
    else if (/[0-7]/.test(escaped)) {
      let octal = escaped;
      for (let count = 0; count < 2 && /[0-7]/.test(value[index + 1] || ""); count += 1) {
        octal += value[index + 1];
        index += 1;
      }
      bytes.push(parseInt(octal, 8));
    } else if (escaped) {
      bytes.push(escaped.charCodeAt(0) & 0xff);
    }
  }
  return Buffer.from(bytes);
}

function decodeUtf16Be(bytes) {
  if (bytes.length % 2 !== 0) return "";
  let text = "";
  for (let index = 0; index < bytes.length; index += 2) {
    const code = bytes.readUInt16BE(index);
    if (code === 0) continue;
    text += String.fromCodePoint(code);
  }
  return text;
}

function hasUsefulText(text) {
  return /[0-9]{2,}|[\u3040-\u30ff\u3400-\u9fff\uff10-\uff19]/.test(text);
}

function decodePdfString(bytes) {
  const candidates = [
    bytes.toString("utf8"),
    decodeUtf16Be(bytes),
    bytes.toString("utf16le"),
    bytes.toString("latin1")
  ];
  return candidates.find(hasUsefulText) || candidates[0] || "";
}

function extractPdfText(file) {
  const buffer = fs.readFileSync(file);
  const parts = [buffer.toString("utf8"), buffer.toString("latin1")];

  for (const bytes of extractPdfStreams(buffer)) {
    const streamText = bytes.toString("latin1");
    parts.push(bytes.toString("utf8"), streamText);

    for (const match of streamText.matchAll(/\((?:\\.|[^\\)])*\)/g)) {
      parts.push(decodePdfString(parsePdfLiteral(match[0].slice(1, -1))));
    }
    for (const match of streamText.matchAll(/<([0-9A-Fa-f]{4,})>\s*Tj/g)) {
      parts.push(decodePdfString(Buffer.from(match[1], "hex")));
    }
  }

  return normalizeText(parts.join(" "));
}

function extractDocumentText(file, format) {
  if (format === "pdf") {
    return extractPdfText(file);
  }
  const buffer = fs.readFileSync(file);
  return normalizeText(`${buffer.toString("utf8")} ${buffer.toString("latin1")}`);
}

function buildLegalFileBase({ serviceName, partnerName, file, format, textHints = [] }) {
  const documentText = extractDocumentText(file, format);
  const combinedText = normalizeText([...textHints, documentText].filter(Boolean).join(" "));
  const date = extractTransactionDate(combinedText);
  const amount = extractTransactionAmount(combinedText);

  if (!date || !amount) {
    const missing = [
      !date ? "\u53d6\u5f15\u65e5" : null,
      !amount ? "\u53d6\u5f15\u91d1\u984d" : null
    ].filter(Boolean).join("\u30fb");
    throw new MetadataExtractionError(
      `${serviceName}\u306e${missing}\u3092\u660e\u7d30\u304b\u3089\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002`,
      "\u6cd5\u4ee4\u8981\u4ef6\u306b\u6cbf\u3063\u305f\u30d5\u30a1\u30a4\u30eb\u540d\u3092\u4f5c\u308b\u305f\u3081\u3001\u660e\u7d30\u5185\u306b\u53d6\u5f15\u65e5/\u767a\u884c\u65e5\u3068\u7a0e\u8fbc\u5408\u8a08\u91d1\u984d\u304c\u8aad\u3081\u308b\u72b6\u614b\u3067\u3042\u308b\u5fc5\u8981\u304c\u3042\u308a\u307e\u3059\u3002",
      "FILE_METADATA_NOT_FOUND"
    );
  }

  const partner = safeNamePart(partnerName);
  const amountPart = normalizeAmount(amount.value);
  return {
    baseName: `${date.value}_${partner}_${amountPart}\u5186`,
    date: date.value,
    dateSource: date.source,
    partner,
    amount: amountPart,
    amountSource: amount.source,
    textLength: combinedText.length
  };
}

function legalFilePath({ outputDir, extension, ...metadataArgs }) {
  const metadata = buildLegalFileBase(metadataArgs);
  return {
    metadata,
    filePath: uniquePath(outputDir, metadata.baseName, extension)
  };
}

module.exports = {
  MetadataExtractionError,
  buildLegalFileBase,
  extractDocumentText,
  legalFilePath
};

