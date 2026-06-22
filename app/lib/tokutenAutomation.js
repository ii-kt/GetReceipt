Warning: truncated output (original token count: 12942)
Total output lines: 1207

"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");
const { ManagedEdge } = require("./browser");
const { delay } = require("./cdp");
const { UserActionError } = require("./eposAutomation");
const { ensureDir, sha256 } = require("./store");
const { MetadataExtractionError, legalFilePath } = require("./fileNaming");

function normalizeText(value) {
  return String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
}

function yearMonthKey(year, month) {
  return `${year}-${String(month).padStart(2, "0")}`;
}

function nextMonthTarget(year, month) {
  const date = new Date(Number(year), Number(month), 1);
  return {
    year: date.getFullYear(),
    month: date.getMonth() + 1,
    yearMonth: yearMonthKey(date.getFullYear(), date.getMonth() + 1)
  };
}

function buildSearchQuery(config, year, month) {
  const template = config.mailSearchQueryTemplate || "繝医け繝・Φ縺ｧ繧薙″ {year}蟷ｴ{month}譛亥・ 隲区ｱよ嶌";
  return template
    .replaceAll("{year}", String(year))
    .replaceAll("{month}", String(month))
    .replaceAll("{month2}", String(month).padStart(2, "0"));
}

function safeFileName(name, fallback) {
  const cleaned = path.basename(String(name || ""))
    .normalize("NFKC")
    .replace(/[<>:"/\\|?*\x00-\x1F]/g, "_")
    .replace(/\s+$/g, "")
    .trim();
  return cleaned || fallback;
}

function uniquePreservingPath(dir, fileName) {
  const parsed = path.parse(fileName);
  const base = parsed.name || "tokuten_statement";
  const ext = parsed.ext || ".pdf";
  let candidate = path.join(dir, `${base}${ext}`);
  let index = 2;
  while (fs.existsSync(candidate)) {
    candidate = path.join(dir, `${base}_${index}${ext}`);
    index += 1;
  }
  return candidate;
}

function defaultDownloadsDir() {
  const home = process.env.USERPROFILE || process.env.HOME;
  return home ? path.join(home, "Downloads") : null;
}

async function waitForStableFile(file, stableMs = 500) {
  let lastSize = -1;
  let stableSince = Date.now();
  while (Date.now() - stableSince < stableMs) {
    const stat = fs.statSync(file);
    if (stat.size !== lastSize) {
      lastSize = stat.size;
      stableSince = Date.now();
    }
    await delay(150);
  }
  return fs.statSync(file);
}

function listRecentPdfCandidates(dir, markerMs, year, month) {
  if (!dir || !fs.existsSync(dir)) return [];
  try {
    return fs.readdirSync(dir)
      .map((name) => {
        const filePath = path.join(dir, name);
        const stat = fs.statSync(filePath);
        return { name, filePath, stat };
      })
      .filter((entry) => entry.stat.isFile())
      .filter((entry) => !entry.name.endsWith(".crdownload") && !entry.name.endsWith(".tmp"))
      .filter((entry) => entry.name.toLowerCase().endsWith(".pdf"))
      .filter((entry) => entry.stat.mtimeMs >= markerMs - 1000)
      .filter((entry) => filenameMatchesTarget(entry.name, year, month))
      .sort((a, b) => b.stat.mtimeMs - a.stat.mtimeMs);
  } catch {
    return [];
  }
}

async function waitForTokutenPdfDownload(dirs, markerMs, year, month, timeoutMs = 30000) {
  const end = Date.now() + timeoutMs;
  const uniqueDirs = [...new Set(dirs.filter(Boolean).map((dir) => path.resolve(dir)))];
  while (Date.now() < end) {
    const candidates = uniqueDirs.flatMap((dir) => listRecentPdfCandidates(dir, markerMs, year, month));
    if (candidates[0]) {
      await waitForStableFile(candidates[0].filePath);
      return candidates[0].filePath;
    }
    await delay(250);
  }
  return null;
}

function filenameMatchesTarget(fileName, year, month) {
  const text = normalizeText(fileName);
  const monthNoPad = String(Number(month));
  const monthPad = String(Number(month)).padStart(2, "0");
  return [
    `${year}蟷ｴ${monthNoPad}譛・,
    `${year}蟷ｴ${monthPad}譛・,
    `${year}/${monthNoPad}`,
    `${year}/${monthPad}`,
    `${year}-${monthNoPad}`,
    `${year}-${monthPad}`,
    `${year}${monthPad}`
  ]…10942 tokens truncated…atement(request) {
    const year = Number(request.year);
    const month = Number(request.month);
    const format = String(request.format || "pdf").toLowerCase();
    const force = Boolean(request.force);
    if (!Number.isInteger(year) || year < 2000 || year > 2100 || !Number.isInteger(month) || month < 1 || month > 12) {
      throw new Error("蟇ｾ雎｡蟷ｴ譛医′荳肴ｭ｣縺ｧ縺吶・);
    }
    if (format !== "pdf") {
      throw new Error("繝医け繝・Φ縺ｧ繧薙″縺ｯPDF縺ｮ縺ｿ蟇ｾ蠢懊＠縺ｦ縺・∪縺吶・);
    }

    const ym = yearMonthKey(year, month);
    const paymentTarget = nextMonthTarget(year, month);
    const existing = this.history.hasExistingFile(ym, format, "tokuten");
    if (existing && !force) {
      this.logger.info("繝医け繝・Φ縺ｧ繧薙″隲区ｱよ嶌縺ｯ蜿門ｾ玲ｸ医∩縺ｮ縺溘ａ繧ｹ繧ｭ繝・・縺励∪縺励◆縲・, { yearMonth: ym, filePath: existing.filePath });
      return { status: "skipped", record: existing };
    }

    this.logger.info("繝医け繝・Φ縺ｧ繧薙″隲区ｱよ嶌縺ｮ蜿門ｾ励ｒ髢句ｧ九＠縺ｾ縺吶・, {
      yearMonth: ym,
      searchYearMonth: paymentTarget.yearMonth
    });
    this.edge.clearDownloadDir();
    await this.edge.ensureStarted();
    await this.edge.navigate(this.targetUrl, 2500);
    await this.edge.bringToFront();
    await this.waitForMailbox();
    const initialAction = await this.searchTargetMail(paymentTarget.year, paymentTarget.month);

    const downloadedFile = await this.downloadAttachmentFromCurrentSearch(paymentTarget.year, paymentTarget.month, initialAction);
    assertPdf(downloadedFile);

    const originalFileName = safeFileName(path.basename(downloadedFile), `tokuten_${paymentTarget.yearMonth}.pdf`);
    if (!filenameMatchesTarget(originalFileName, paymentTarget.year, paymentTarget.month)) {
      throw new UserActionError(
        `繝繧ｦ繝ｳ繝ｭ繝ｼ繝峨＠縺蘖DF縺ｮ繝輔ぃ繧､繝ｫ蜷阪′謾ｯ謇輔＞蟷ｴ譛・${paymentTarget.yearMonth} 縺ｨ荳閾ｴ縺励∪縺帙ｓ縲Ａ,
        `隱､蜿門ｾ励ｒ驕ｿ縺代ｋ縺溘ａ菫晏ｭ倥ｒ蛛懈ｭ｢縺励∪縺励◆縲ゅム繧ｦ繝ｳ繝ｭ繝ｼ繝峨＆繧後◆繝輔ぃ繧､繝ｫ蜷・ ${originalFileName}`,
        "DOWNLOADED_FILE_MONTH_MISMATCH"
      );
    }

    let naming;
    try {
      const summaryAfterDownload = await this.edge.pageSummary().catch(() => null);
      const ocrText = extractTokutenOcrText(downloadedFile, this.stagingDir);
      naming = legalFilePath({
        serviceName: "繝医け繝・Φ縺ｧ繧薙″",
        partnerName: this.config.transactionPartnerName || "繝輔Λ繝・ヨ繧ｨ繝翫ず繝ｼ譬ｪ蠑丈ｼ夂､ｾ",
        file: downloadedFile,
        format,
        outputDir: this.outputDir,
        extension: ".pdf",
        textHints: [
          originalFileName,
          summaryAfterDownload?.text,
          ocrText
        ]
      });
    } catch (error) {
      if (error instanceof MetadataExtractionError) {
        throw new UserActionError(error.message, error.advice, error.code);
      }
      throw error;
    }

    const finalPath = naming.filePath;
    fs.renameSync(downloadedFile, finalPath);
    const stat = fs.statSync(finalPath);
    const record = this.history.add({
      service: "tokuten",
      yearMonth: ym,
      format,
      fileName: path.basename(finalPath),
      filePath: finalPath,
      originalFileName,
      naming: naming.metadata,
      size: stat.size,
      sha256: sha256(finalPath),
      sourceUrl: this.targetUrl
    });

    this.logger.info("繝医け繝・Φ縺ｧ繧薙″隲区ｱよ嶌PDF繧剃ｿ晏ｭ倥＠縺ｾ縺励◆縲・, { filePath: finalPath, size: stat.size });
    return { status: "saved", record };
  }

  async shutdown() {
    await this.edge.closeAutomationBrowser();
  }
}

module.exports = {
  TokutenAutomation,
  classifyTokutenLoginState
};

