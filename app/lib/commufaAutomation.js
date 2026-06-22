Warning: truncated output (original token count: 11356)
Total output lines: 959

"use strict";

const fs = require("fs");
const path = require("path");
const { ManagedEdge } = require("./browser");
const { delay } = require("./cdp");
const { UserActionError } = require("./eposAutomation");
const { ensureDir, sha256, uniquePath } = require("./store");
const { MetadataExtractionError, extractDocumentText, legalFilePath } = require("./fileNaming");

function normalizeText(value) {
  return String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
}

function classifyCommufaLoginState(summary, config) {
  const text = normalizeText(`${summary.title || ""} ${summary.url || ""} ${summary.text || ""}`);
  const loginScore = (config.loginHints || []).filter((hint) => text.includes(normalizeText(hint))).length;
  const loggedInScore = (config.loggedInHints || []).filter((hint) => text.includes(normalizeText(hint))).length;
  const pageScore = (config.monthPageHints || []).filter((hint) => text.includes(normalizeText(hint))).length;
  const hasPassword = Number(summary.passwordFields || 0) > 0;
  const loginLikeUrl = /login|signin|auth|join\/s/i.test(summary.url || "");

  if (hasPassword || (loginLikeUrl && loginScore > 0 && loggedInScore === 0)) {
    return {
      state: "login-required",
      label: "繝ｭ繧ｰ繧､繝ｳ蠕・■",
      reason: "繝ｭ繧ｰ繧､繝ｳ逕ｻ髱｢縲√∪縺溘・繝代せ繝ｯ繝ｼ繝牙・蜉帶ｬ・ｒ讀懃衍縺励∪縺励◆縲・
    };
  }

  if (loggedInScore > 0 || pageScore > 0) {
    return {
      state: "logged-in",
      label: "繝ｭ繧ｰ繧､繝ｳ貂医∩",
      reason: "繝ｭ繧ｰ繧､繝ｳ蠕後・繝壹・繧ｸ繧峨＠縺・枚險繧呈､懃衍縺励∪縺励◆縲・
    };
  }

  return {
    state: "unknown",
    label: "蛻､螳壻ｸｭ",
    reason: "繝ｭ繧ｰ繧､繝ｳ迥ｶ諷九ｒ縺ｾ縺蛻､螳壹〒縺阪∪縺帙ｓ縲・
  };
}

function buildCommufaAutoLoginExpression() {
  return `(() => {
    const normalize = (value) => String(value || "")
      .normalize("NFKC")
      .replace(/\\s+/g, " ")
      .trim()
      .toLowerCase();

    const visible = (el) => {
      if (!el || el.disabled) return false;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== "none" &&
        style.visibility !== "hidden" &&
        rect.width > 0 &&
        rect.height > 0;
    };

    const labelOf = (el) => [
      el.innerText,
      el.textContent,
      el.value,
      el.title,
      el.getAttribute && el.getAttribute("aria-label"),
      el.getAttribute && el.getAttribute("name"),
      el.getAttribute && el.getAttribute("id")
    ].filter(Boolean).join(" ");

    const passwordInput = [...document.querySelectorAll("input[type='password']")]
      .find(visible);
    if (!passwordInput) {
      return { attempted: false, reason: "繝ｭ繧ｰ繧､繝ｳ逕ｻ髱｢縺ｧ縺ｯ縺ゅｊ縺ｾ縺帙ｓ縲・ };
    }

    const idInput = [...document.querySelectorAll("input")]
      .filter(visible)
      .find((input) => {
        const type = String(input.type || "text").toLowerCase();
        return ["text", "email", "tel", "search"].includes(type) &&
          String(input.value || "").trim().length > 0;
      });
    if (!idInput || String(passwordInput.value || "").length === 0) {
      return { attempted: false, reason: "繝ｭ繧ｰ繧､繝ｳ諠・ｱ縺梧悴蜈･蜉帙〒縺吶・ };
    }

    const loginButton = [...document.querySelectorAll("button, input[type='button'], input[type='submit'], [role='button']")]
      .filter(visible)
      .map((el) => ({ el, text: normalize(labelOf(el)) }))
      .filter((item) => item.text.includes(normalize("繝ｭ繧ｰ繧､繝ｳ")))
      .filter((item) => !item.text.includes("apple") && !item.text.includes("line"))
      .filter((item) => !item.text.includes(normalize("繝ｭ繧ｰ繧､繝ｳID逋ｻ骭ｲ")) && !item.text.includes(normalize("縺雁ｿ倥ｌ")))
   …9356 tokens truncated…蟇ｾ雎｡蟷ｴ譛医・隲区ｱよ・邏ｰ縺瑚｡ｨ遉ｺ縺輔ｌ縺ｦ縺・ｋ縺狗｢ｺ隱阪＠縺ｦ縺上□縺輔＞縲・,
        action.code || "DOWNLOAD_BUTTON_NOT_FOUND"
      );
    }

    let downloadedFile = null;
    let metadataText = "";
    if (action.expectsDownload !== false) {
      downloadedFile = await this.edge.waitForDownload(format, markerMs, 45000);
    }

    if (!downloadedFile && format === "pdf" && action.fallbackPrint) {
      await delay(300);
      const switchedToPrint = await this.edge.switchToPage((target) => {
        const text = `${target.url || ""} ${target.title || ""}`.toLowerCase();
        return text.includes("print=1") || text.includes("print") || text.includes("蜊ｰ蛻ｷ");
      });
      if (!switchedToPrint) {
        const meiym = `${year}${String(month).padStart(2, "0")}`;
        await this.edge.switchToPage((target) => {
          const text = `${target.url || ""} ${target.title || ""}`.toLowerCase();
          return text.includes("cw40001") && text.includes(`meiym=${meiym}`);
        });
      }
      const summaryBeforePrint = await this.edge.pageSummary().catch(() => null);
      metadataText = summaryBeforePrint?.text || "";
      downloadedFile = path.join(this.stagingDir, `commufa-print-${Date.now()}.pdf`);
      await this.edge.printToPdf(downloadedFile);
      this.logger.info("繧ｳ繝溘Η繝輔ぃ譏守ｴｰ繝壹・繧ｸ繧単DF縺ｨ縺励※菫晏ｭ倥＠縺ｾ縺励◆縲・);
    }

    if (!downloadedFile) {
      throw new UserActionError(
        "繧ｳ繝溘Η繝輔ぃ譏守ｴｰ縺ｮ繝繧ｦ繝ｳ繝ｭ繝ｼ繝牙ｮ御ｺ・ｒ讀懃衍縺ｧ縺阪∪縺帙ｓ縺ｧ縺励◆縲・,
        format === "csv"
          ? "繧ｳ繝溘Η繝輔ぃ逕ｻ髱｢縺靴SV蜃ｺ蜉帙↓蟇ｾ蠢懊＠縺ｦ縺・ｋ縺狗｢ｺ隱阪＠縺ｦ縺上□縺輔＞縲１DF蠖｢蠑上〒縺ｮ蜿門ｾ励ｂ隧ｦ縺励※縺上□縺輔＞縲・
          : "蜿門ｾ礼畑繝悶Λ繧ｦ繧ｶ逕ｻ髱｢縺ｧ遒ｺ隱阪ム繧､繧｢繝ｭ繧ｰ縲√・繝・・繧｢繝・・縲√∪縺溘・PDF繝励Ξ繝薙Η繝ｼ縺碁幕縺・※縺・↑縺・°遒ｺ隱阪＠縺ｦ縺上□縺輔＞縲・,
        "DOWNLOAD_TIMEOUT"
      );
    }

    assertDownloadedContent(downloadedFile, format);
    assertCommufaUsageMonth(downloadedFile, format, year, month);

    let naming;
    try {
      const summaryAfterDownload = await this.edge.pageSummary().catch(() => null);
      naming = legalFilePath({
        serviceName: "繧ｳ繝溘Η繝輔ぃ",
        partnerName: this.config.transactionPartnerName || "荳ｭ驛ｨ繝・Ξ繧ｳ繝溘Η繝九こ繝ｼ繧ｷ繝ｧ繝ｳ譬ｪ蠑丈ｼ夂､ｾ",
        file: downloadedFile,
        format,
        outputDir: this.outputDir,
        extension: `.${format}`,
        textHints: [
          metadataText,
          summaryAfterDownload?.text
        ]
      });
    } catch (error) {
      if (error instanceof MetadataExtractionError) {
        throw new UserActionError(error.message, error.advice, error.code);
      }
      throw error;
    }

    const originalFileName = path.basename(downloadedFile);
    const finalPath = naming.filePath;
    fs.renameSync(downloadedFile, finalPath);
    const stat = fs.statSync(finalPath);
    const record = this.history.add({
      service: "commufa",
      yearMonth,
      format,
      fileName: path.basename(finalPath),
      filePath: finalPath,
      originalFileName,
      naming: naming.metadata,
      size: stat.size,
      sha256: sha256(finalPath),
      sourceUrl: this.targetUrl
    });

    this.logger.info("繧ｳ繝溘Η繝輔ぃ譏守ｴｰ繝輔ぃ繧､繝ｫ繧剃ｿ晏ｭ倥＠縺ｾ縺励◆縲・, { filePath: finalPath, size: stat.size });
    return { status: "saved", record };
  }

  async shutdown() {
    await this.edge.closeAutomationBrowser();
  }
}

module.exports = {
  CommufaAutomation,
  classifyCommufaLoginState
};

