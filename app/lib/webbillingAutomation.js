Warning: truncated output (original token count: 12292)
Total output lines: 1082

"use strict";

const fs = require("fs");
const path = require("path");
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

function classifyWebBillingLoginState(summary, config) {
  const text = normalizeText(`${summary.title || ""} ${summary.url || ""} ${summary.text || ""}`);
  const loginScore = (config.loginHints || []).filter((hint) => text.includes(normalizeText(hint))).length;
  const loggedInScore = (config.loggedInHints || []).filter((hint) => text.includes(normalizeText(hint))).length;
  const hasPassword = Number(summary.passwordFields || 0) > 0;
  const loginLikeUrl = /login|auth|signin|logon/i.test(summary.url || "");
  const hasDAccountLogin = text.includes(normalizeText("d繧｢繧ｫ繧ｦ繝ｳ繝・)) || text.includes("d account");

  if (loggedInScore === 0 && (hasPassword || hasDAccountLogin || (loginLikeUrl && loginScore > 0))) {
    return {
      state: "login-required",
      label: "繝ｭ繧ｰ繧､繝ｳ蠕・■",
      reason: "繝ｭ繧ｰ繧､繝ｳ逕ｻ髱｢縲√∪縺溘・繝代せ繝ｯ繝ｼ繝牙・蜉帶ｬ・ｒ讀懃衍縺励∪縺励◆縲・
    };
  }

  if (loggedInScore > 0 || text.includes("web繝薙Μ繝ｳ繧ｰ") && text.includes("繝ｭ繧ｰ繧｢繧ｦ繝・)) {
    return {
      state: "logged-in",
      label: "繝ｭ繧ｰ繧､繝ｳ貂医∩",
      reason: "Web繝薙Μ繝ｳ繧ｰ縺ｮ繝ｭ繧ｰ繧､繝ｳ蠕後・繝ｼ繧ｸ繧峨＠縺・枚險繧呈､懃衍縺励∪縺励◆縲・
    };
  }

  return {
    state: "unknown",
    label: "蛻､螳壻ｸｭ",
    reason: "繝ｭ繧ｰ繧､繝ｳ迥ｶ諷九ｒ縺ｾ縺蛻､螳壹〒縺阪∪縺帙ｓ縲・
  };
}

function buildWebBillingAutoLoginExpressionLegacy() {
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
      el.alt,
      el.title,
      el.placeholder,
      el.getAttribute && el.getAttribute("aria-label"),
      el.getAttribute && el.getAttribute("name"),
      el.getAttribute && el.getAttribute("id"),
      ...(el.querySelectorAll ? [...el.querySelectorAll("img,[alt],[title],[aria-label]")]
        .flatMap((child) => [child.alt, child.title, child.getAttribute && child.getAttribute("aria-label")]) : [])
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

    const loginButton = [...document.querySelectorAll("button, input[type='butto…10292 tokens truncated…   if (lastAction.click) {
        const markerMs = Date.now();
        await this.edge.clickAt(lastAction.click.x, lastAction.click.y);
        if (lastAction.expectsDownload || lastAction.mayDownload) {
          downloadedFile = await this.edge.waitForDownload("pdf", markerMs, lastAction.expectsDownload ? 60000 : 3500);
          if (downloadedFile) {
            break;
          }
          if (lastAction.expectsDownload) {
            throw new UserActionError(
              "Web繝薙Μ繝ｳ繧ｰ險ｼ譏取嶌PDF縺ｮ繝繧ｦ繝ｳ繝ｭ繝ｼ繝牙ｮ御ｺ・ｒ讀懃衍縺ｧ縺阪∪縺帙ｓ縺ｧ縺励◆縲・,
              "譛邨ゅム繧ｦ繝ｳ繝ｭ繝ｼ繝峨・繧ｿ繝ｳ縺ｯ謚ｼ縺励∪縺励◆縺訓DF繝輔ぃ繧､繝ｫ縺御ｿ晏ｭ倥＆繧後∪縺帙ｓ縺ｧ縺励◆縲ょ叙蠕礼畑繝悶Λ繧ｦ繧ｶ逕ｻ髱｢縺ｧ繝繧ｦ繝ｳ繝ｭ繝ｼ繝臥｢ｺ隱阪ｄ繝悶Ο繝・け陦ｨ遉ｺ縺悟・縺ｦ縺・↑縺・°遒ｺ隱阪＠縺ｦ縺上□縺輔＞縲・,
              "DOWNLOAD_TIMEOUT"
            );
          }
        }
        await delay(Math.min(lastAction.waitMs || 1200, 1600));
        await this.focusWebBillingPage();
        continue;
      }

      throw new UserActionError(
        lastAction.message || "Web繝薙Μ繝ｳ繧ｰ險ｼ譏取嶌縺ｮ蜿門ｾ玲桃菴懊ｒ騾ｲ繧√ｉ繧後∪縺帙ｓ縺ｧ縺励◆縲・,
        lastAction.advice || "Web繝薙Μ繝ｳ繧ｰ逕ｻ髱｢縺ｧ蟇ｾ雎｡譛医・險ｼ譏取嶌陦後→繝繧ｦ繝ｳ繝ｭ繝ｼ繝画桃菴懊′陦ｨ遉ｺ縺輔ｌ縺ｦ縺・ｋ縺狗｢ｺ隱阪＠縺ｦ縺上□縺輔＞縲・,
        lastAction.code || "WEBBILLING_ACTION_NOT_FOUND"
      );
    }

    if (!downloadedFile) {
      throw new UserActionError(
        "Web繝薙Μ繝ｳ繧ｰ險ｼ譏取嶌PDF縺ｮ繝繧ｦ繝ｳ繝ｭ繝ｼ繝牙ｮ御ｺ・ｒ讀懃衍縺ｧ縺阪∪縺帙ｓ縺ｧ縺励◆縲・,
        "蟇ｾ雎｡譛医メ繧ｧ繝・け縲∵ｬ｡縺ｸ縲∝酔諢上√ム繧ｦ繝ｳ繝ｭ繝ｼ繝峨・縺・★繧後°縺ｧ逕ｻ髱｢驕ｷ遘ｻ縺梧ｭ｢縺ｾ縺｣縺溷庄閭ｽ諤ｧ縺後≠繧翫∪縺吶ょ叙蠕礼畑繝悶Λ繧ｦ繧ｶ逕ｻ髱｢縺ｮ迥ｶ諷九ｒ遒ｺ隱阪＠縺ｦ縺九ｉ蜀榊ｮ溯｡後＠縺ｦ縺上□縺輔＞縲・,
        lastAction?.code || "DOWNLOAD_TIMEOUT"
      );
    }

    assertPdf(downloadedFile);

    let naming;
    try {
      const summaryAfterDownload = await this.edge.pageSummary().catch(() => null);
      naming = legalFilePath({
        serviceName: "謳ｺ蟶ｯ譁咎≡",
        partnerName: this.config.transactionPartnerName || "譬ｪ蠑丈ｼ夂､ｾNTT繝峨さ繝｢",
        file: downloadedFile,
        format,
        outputDir: this.outputDir,
        extension: ".pdf",
        textHints: [
          ...metadataHints,
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
      service: "mobile",
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

    this.logger.info("謳ｺ蟶ｯ譁咎≡險ｼ譏取嶌PDF繧剃ｿ晏ｭ倥＠縺ｾ縺励◆縲・, { filePath: finalPath, size: stat.size });
    return { status: "saved", record };
  }

  async shutdown() {
    await this.edge.closeAutomationBrowser();
  }
}

module.exports = {
  WebBillingAutomation,
  classifyWebBillingLoginState
};

