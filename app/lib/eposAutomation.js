Warning: truncated output (original token count: 8322)
Total output lines: 729

"use strict";

const fs = require("fs");
const path = require("path");
const { ManagedEdge } = require("./browser");
const { delay } = require("./cdp");
const { ensureDir, sha256, uniquePath } = require("./store");
const { MetadataExtractionError, legalFilePath } = require("./fileNaming");

class UserActionError extends Error {
  constructor(message, advice, code = "USER_ACTION_NEEDED") {
    super(message);
    this.name = "UserActionError";
    this.advice = advice;
    this.code = code;
  }
}

function normalizeText(value) {
  return String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase();
}

function classifyLoginState(summary, config) {
  const text = normalizeText(`${summary.title || ""} ${summary.url || ""} ${summary.text || ""}`);
  const loginScore = (config.loginHints || []).filter((hint) => text.includes(normalizeText(hint))).length;
  const loggedInScore = (config.loggedInHints || []).filter((hint) => text.includes(normalizeText(hint))).length;
  const pageScore = (config.monthPageHints || []).filter((hint) => text.includes(normalizeText(hint))).length;
  const hasPassword = Number(summary.passwordFields || 0) > 0;
  const loginLikeUrl = /login|auth|signin|logon/i.test(summary.url || "");
  const targetLikeUrl = /nocardusedetail|usedetail|memberservice/i.test(summary.url || "");

  if (hasPassword || (loginLikeUrl && loginScore > 0)) {
    return {
      state: "login-required",
      label: "繝ｭ繧ｰ繧､繝ｳ蠕・■",
      reason: "繝ｭ繧ｰ繧､繝ｳ逕ｻ髱｢縲√∪縺溘・繝代せ繝ｯ繝ｼ繝牙・蜉帶ｬ・ｒ讀懃衍縺励∪縺励◆縲・
    };
  }

  if (loggedInScore > 0 || pageScore > 0 || (targetLikeUrl && loginScore === 0)) {
    return {
      state: "logged-in",
      label: "繝ｭ繧ｰ繧､繝ｳ貂医∩",
      reason: "繝ｭ繧ｰ繧､繝ｳ蠕後・繝ｼ繧ｸ繧峨＠縺・枚險繧呈､懃衍縺励∪縺励◆縲・
    };
  }

  return {
    state: "unknown",
    label: "蛻､螳壻ｸｭ",
    reason: "繝ｭ繧ｰ繧､繝ｳ迥ｶ諷九ｒ縺ｾ縺蛻､螳壹〒縺阪∪縺帙ｓ縲・
  };
}

function buildEposAutoLoginExpression() {
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
      el.getAttribute && el.getAttribute("aria-label"),
      el.getAttribute && el.getAttribute("name"),
      el.getAttribute && el.getAttribute("id")
    ].filter(Boolean).join(" ");

    const pageText = normalize(document.body?.innerText || "");
    if (pageText.includes(normalize("\\u753b\\u50cf\\u8a8d\\u8a3c")) || pageText.includes(normalize("\\u30d1\\u30ba\\u30eb"))) {
      return { attempted: false, reason: "\\u753b\\u50cf\\u8a8d\\u8a3c\\u307e\\u305f\\u306f\\u30d1\\u30ba\\u30eb\\u8a8d\\u8a3c\\u304c\\u8868\\u793a\\u3055\\u308c\\u3066\\u3044\\u307e\\u3059\\u3002" };
    }

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
      return {…6322 tokens truncated…｡蟷ｴ譛医′繧ｨ繝昴せNet逕ｻ髱｢縺ｫ蟄伜惠縺励∪縺帙ｓ縺ｧ縺励◆縲・
        : "蜿門ｾ励・繧ｿ繝ｳ繧定ｦ九▽縺代ｉ繧後∪縺帙ｓ縺ｧ縺励◆縲・;
      this.logger.warn(warnMessage, action);
      throw new UserActionError(
        action.message || "蜿門ｾ励・繧ｿ繝ｳ繧定ｦ九▽縺代ｉ繧後∪縺帙ｓ縺ｧ縺励◆縲・,
        action.advice || "繧ｨ繝昴せNet逕ｻ髱｢縺ｧ蟇ｾ雎｡蟷ｴ譛医・譏守ｴｰ繝壹・繧ｸ縺瑚｡ｨ遉ｺ縺輔ｌ縺ｦ縺・ｋ縺狗｢ｺ隱阪＠縺ｦ縺上□縺輔＞縲ゅし繧､繝亥・縺ｮ繝懊ち繝ｳ蜷阪′螟峨ｏ縺｣縺ｦ縺・ｋ蝣ｴ蜷医・ app/config/siteSelectors.json 繧定ｪｿ謨ｴ縺ｧ縺阪∪縺吶・,
        action.code || "DOWNLOAD_BUTTON_NOT_FOUND"
      );
    }

    let downloadedFile = await this.edge.waitForDownload(format, markerMs, 90000);
    if (!downloadedFile) {
      this.logger.warn("騾壼ｸｸ縺ｮ繝繧ｦ繝ｳ繝ｭ繝ｼ繝峨ｒ讀懃衍縺ｧ縺阪∪縺帙ｓ縺ｧ縺励◆縲ゅヶ繝ｩ繧ｦ繧ｶ荳翫↓髢九＞縺溘ヵ繧｡繧､繝ｫURL繧堤｢ｺ隱阪＠縺ｾ縺吶・);
      const targets = await this.edge.getTargets();
      const candidate = targets
        .filter((target) => target.type === "page")
        .map((target) => target.url)
        .filter((url) => /^https?:\/\//i.test(url))
        .find((url) => url.toLowerCase().includes(format) || /download|csv|pdf|meisai|detail|receipt/i.test(url));
      if (candidate) {
        const fetched = path.join(this.stagingDir, `fetched.${format}`);
        const ok = await fetchProtectedResource(this.edge, candidate, format, fetched, this.logger);
        if (ok) {
          downloadedFile = fetched;
        }
      }
    }

    if (!downloadedFile) {
      throw new UserActionError(
        "繝繧ｦ繝ｳ繝ｭ繝ｼ繝牙ｮ御ｺ・ｒ讀懃衍縺ｧ縺阪∪縺帙ｓ縺ｧ縺励◆縲・,
        "蜿門ｾ礼畑繝悶Λ繧ｦ繧ｶ逕ｻ髱｢縺ｧ遒ｺ隱阪ム繧､繧｢繝ｭ繧ｰ縲√・繝・・繧｢繝・・縲√∪縺溘・PDF繝励Ξ繝薙Η繝ｼ縺碁幕縺・※縺・↑縺・°遒ｺ隱阪＠縺ｦ縺上□縺輔＞縲る幕縺・※縺・ｋ蝣ｴ蜷医・髢峨§縺壹↓蜀榊ｮ溯｡後＠縺ｦ縺上□縺輔＞縲・,
        "DOWNLOAD_TIMEOUT"
      );
    }

    assertDownloadedContent(downloadedFile, format);

    let naming;
    try {
      const summaryAfterDownload = await this.edge.pageSummary().catch(() => null);
      naming = legalFilePath({
        serviceName: "EPOS",
        partnerName: this.config.transactionPartnerName || "譬ｪ蠑丈ｼ夂､ｾ繧ｨ繝昴せ繧ｫ繝ｼ繝・,
        file: downloadedFile,
        format,
        outputDir: this.outputDir,
        extension: `.${format}`,
        textHints: [
          action.metadataText,
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
      service: "epos",
      yearMonth,
      format,
      fileName: path.basename(finalPath),
      filePath: finalPath,
      originalFileName,
      naming: naming.metadata,
      size: stat.size,
      sha256: sha256(finalPath),
      sourceUrl: stateNow.summary.url
    });

    this.logger.info("譏守ｴｰ繝輔ぃ繧､繝ｫ繧剃ｿ晏ｭ倥＠縺ｾ縺励◆縲・, { filePath: finalPath, size: stat.size });
    return { status: "saved", record };
  }

  async shutdown() {
    await this.edge.closeAutomationBrowser();
  }
}

module.exports = {
  EposAutomation,
  UserActionError,
  classifyLoginState
};

