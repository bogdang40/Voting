document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("scan-form");
  if (!form) return;

  const slug = form.dataset.slug;
  const csrf = document.querySelector("meta[name='csrf-token']")?.content || "";

  const hiddenInput = document.getElementById("hidden-file-input");
  const captureConfidenceInput = document.getElementById("capture-confidence");
  const captureModeInput = document.getElementById("capture-mode");
  const operatorOverrideInput = document.getElementById("operator-override");

  const tabButtons = Array.from(document.querySelectorAll(".tab-btn[data-tab]"));
  const tabPanels = {
    camera: document.getElementById("tab-camera"),
    upload: document.getElementById("tab-upload"),
  };

  const steps = Array.from(document.querySelectorAll(".step[data-step]"));
  const qualityPanel = document.getElementById("quality-panel");
  const qualityState = document.getElementById("quality-state");
  const qualityList = document.getElementById("quality-list");
  const qualityMetrics = document.getElementById("quality-metrics");

  const video = document.getElementById("cam-video");
  const canvas = document.getElementById("cam-canvas");
  const camLive = document.getElementById("cam-live");
  const liveWrap = camLive?.querySelector(".camera-wrap");
  const camCaptured = document.getElementById("cam-captured");
  const camPreview = document.getElementById("cam-preview-img");
  const startBtn = document.getElementById("btn-start-cam");
  const autoBtn = document.getElementById("btn-auto-capture");
  const captureBtn = document.getElementById("btn-capture-cam");
  const retakeBtn = document.getElementById("btn-retake");
  const btnScanCam = document.getElementById("btn-scan-cam");
  const btnTogglePreviewSource = document.getElementById("btn-toggle-preview-source");

  const liveStatus = document.getElementById("live-detect-status");
  const markerDots = {
    tl: document.getElementById("marker-tl"),
    tr: document.getElementById("marker-tr"),
    bl: document.getElementById("marker-bl"),
    br: document.getElementById("marker-br"),
  };

  const captureReview = document.getElementById("capture-review");
  const confidenceReview = document.getElementById("confidence-review");
  const overrideWrap = document.getElementById("override-wrap");
  const overrideCheckbox = document.getElementById("override-low-confidence");

  const uploadInput = document.getElementById("upload-file-input");
  const uploadDrop = document.getElementById("upload-drop");
  const uploadPreviewWrap = document.getElementById("upload-preview-wrap");
  const uploadPreview = document.getElementById("upload-preview-img");
  const btnScanUpload = document.getElementById("btn-scan-upload");
  const cameraWarning = document.getElementById("camera-capability-warning");

  const spinnerCam = document.getElementById("spinner-cam");
  const spinnerUpload = document.getElementById("spinner-upload");

  const reasonMap = {
    very_blurry: "Imagine foarte blurata",
    blurry: "Imagine usor blurata",
    too_dark: "Lumina insuficienta",
    too_bright: "Imagine supraexpusa",
    frame_missing: "Buletin incomplet in cadru",
  };

  const lowConfidenceThreshold = 65;
  let currentTab = "camera";
  let stream = null;
  let liveProbeTimer = null;
  let liveProbeInFlight = false;
  let autoCaptureEnabled = true;
  let stableAlignedFrames = 0;
  let stableTargetFrames = 3;
  let qualityStatus = "ok";
  let latestLiveScore = 0;
  let latestMarkerNorm = null;
  let latestMarkerSource = null;
  let captureInProgress = false;
  let selectedBallotFile = null;

  let originalBlob = null;
  let correctedBlob = null;
  let usingCorrectedPreview = false;
  let originalPreviewUrl = null;
  let correctedPreviewUrl = null;

  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

  const setStep = (active, completed = []) => {
    steps.forEach((step) => {
      const n = Number(step.dataset.step);
      step.classList.remove("active", "done");
      if (completed.includes(n)) step.classList.add("done");
      if (n === active) step.classList.add("active");
    });
  };

  const setLiveStatus = (text, tone = "") => {
    if (!liveStatus) return;
    liveStatus.textContent = text;
    liveStatus.classList.remove("ok", "warn", "error");
    if (tone) liveStatus.classList.add(tone);
  };

  const setMarkerDotState = (key, state) => {
    const dot = markerDots[key];
    if (!dot) return;
    dot.classList.remove("ok", "warn", "off");
    if (state === "ok") dot.classList.add("ok");
    if (state === "warn") dot.classList.add("warn");
    if (!state) dot.classList.add("off");
  };

  const clearMarkerDotPosition = (key) => {
    const dot = markerDots[key];
    if (!dot) return;
    dot.style.removeProperty("left");
    dot.style.removeProperty("top");
    dot.style.removeProperty("right");
    dot.style.removeProperty("bottom");
    dot.style.visibility = "hidden";
  };

  const mapNormToLiveOverlay = (norm, sourceW, sourceH) => {
    if (!norm || !liveWrap) return null;
    const wrapW = liveWrap.clientWidth || 0;
    const wrapH = liveWrap.clientHeight || 0;
    if (!wrapW || !wrapH || !sourceW || !sourceH) return null;

    // Match CSS object-fit: cover used by the live video.
    const scale = Math.max(wrapW / sourceW, wrapH / sourceH);
    const renderW = sourceW * scale;
    const renderH = sourceH * scale;
    const offsetX = (wrapW - renderW) / 2;
    const offsetY = (wrapH - renderH) / 2;

    return {
      x: clamp(norm.x * sourceW * scale + offsetX, 0, wrapW),
      y: clamp(norm.y * sourceH * scale + offsetY, 0, wrapH),
    };
  };

  const positionMarkerDots = (markerNorm, sourceW, sourceH) => {
    if (!markerNorm) {
      ["tl", "tr", "bl", "br"].forEach((k) => clearMarkerDotPosition(k));
      return;
    }

    ["tl", "tr", "bl", "br"].forEach((k) => {
      const dot = markerDots[k];
      const norm = markerNorm[k];
      if (!dot || !norm) {
        clearMarkerDotPosition(k);
        return;
      }

      const pt = mapNormToLiveOverlay(norm, sourceW, sourceH);
      if (!pt) {
        clearMarkerDotPosition(k);
        return;
      }

      dot.style.left = `${pt.x}px`;
      dot.style.top = `${pt.y}px`;
      dot.style.right = "auto";
      dot.style.bottom = "auto";
      dot.style.visibility = "visible";
    });
  };

  const resetMarkerDots = () => {
    ["tl", "tr", "bl", "br"].forEach((k) => {
      setMarkerDotState(k, "");
      clearMarkerDotPosition(k);
    });
  };

  const resetCaptureMeta = () => {
    if (captureConfidenceInput) captureConfidenceInput.value = "";
    if (captureModeInput) captureModeInput.value = "unknown";
    if (operatorOverrideInput) operatorOverrideInput.value = "0";
    if (overrideCheckbox) overrideCheckbox.checked = false;
    if (overrideWrap) overrideWrap.hidden = true;
    latestLiveScore = 0;
    selectedBallotFile = null;
  };

  const renderQuality = (quality) => {
    qualityStatus = quality?.status || "ok";
    qualityPanel?.removeAttribute("hidden");

    if (qualityState) {
      qualityState.className = "badge";
      if (qualityStatus === "ok") {
        qualityState.classList.add("ok");
        qualityState.textContent = "Calitate OK";
      } else if (qualityStatus === "warn") {
        qualityState.classList.add("warn");
        qualityState.textContent = "Calitate medie";
      } else {
        qualityState.classList.add("error");
        qualityState.textContent = "Calitate slaba";
      }
    }

    if (qualityList) {
      const reasons = Array.isArray(quality?.reasons) ? quality.reasons : [];
      qualityList.innerHTML = reasons.length
        ? reasons.map((reason) => `<li>${reasonMap[reason] || reason}</li>`).join("")
        : "<li>Nicio problema detectata.</li>";
    }

    if (qualityMetrics) {
      const m = quality?.metrics || {};
      const parts = [];
      if (typeof m.blur_score === "number") parts.push(`Claritate: ${m.blur_score}`);
      if (typeof m.brightness === "number") parts.push(`Luminozitate: ${m.brightness}`);
      if (typeof m.frame_ratio === "number") parts.push(`Cadru: ${Math.round(m.frame_ratio * 100)}%`);
      qualityMetrics.textContent = parts.join(" | ");
    }
  };

  const setInputFile = (input, file) => {
    if (!input || !file) return false;
    try {
      if (typeof DataTransfer !== "undefined") {
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
        return true;
      }
    } catch (_) {
      // Safari/older browsers may block programmatic file assignment.
    }
    return false;
  };

  const setHiddenFile = (file) => {
    if (!file) return;
    selectedBallotFile = file;
    setInputFile(hiddenInput, file);
  };

  const scoreFromQuality = (quality) => {
    if (!quality || !quality.status) return 75;
    if (quality.status === "ok") return 88;
    if (quality.status === "warn") return 72;
    return 45;
  };

  const setUploadSubmitEnabled = (enabled) => {
    if (btnScanUpload) btnScanUpload.disabled = !enabled;
  };

  const updateCameraSubmitEnabled = () => {
    if (!btnScanCam) return;
    const score = Number(captureConfidenceInput?.value || 0);
    const needsOverride = score > 0 && score < lowConfidenceThreshold;
    const hasOverride = overrideCheckbox?.checked;
    const okByQuality = qualityStatus !== "fail";
    btnScanCam.disabled = !(okByQuality && (!needsOverride || hasOverride));

    if (operatorOverrideInput) {
      operatorOverrideInput.value = hasOverride ? "1" : "0";
    }
  };

  const setCaptureConfidence = (score) => {
    const safe = Math.round(clamp(score, 0, 100));
    if (captureConfidenceInput) captureConfidenceInput.value = String(safe);

    if (confidenceReview) {
      confidenceReview.textContent = `Scor incredere captura: ${safe}/100`;
    }

    if (overrideWrap) {
      overrideWrap.hidden = safe >= lowConfidenceThreshold;
    }

    if (safe >= lowConfidenceThreshold && overrideCheckbox) {
      overrideCheckbox.checked = false;
      if (operatorOverrideInput) operatorOverrideInput.value = "0";
    }

    updateCameraSubmitEnabled();
  };

  const canvasToBlob = (cnv, mime = "image/jpeg", quality = 0.95) =>
    new Promise((resolve) => cnv.toBlob(resolve, mime, quality));

  const blobToImage = (blob) =>
    new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(blob);
      img.onload = () => {
        URL.revokeObjectURL(url);
        resolve(img);
      };
      img.onerror = (err) => {
        URL.revokeObjectURL(url);
        reject(err);
      };
      img.src = url;
    });

  const toJpegFile = async (file) => {
    if (!file || !String(file.type || "").startsWith("image/")) return file;

    // Already jpeg/jpg and normal size: keep original bytes.
    if (/jpe?g/i.test(file.type) && file.size <= 8 * 1024 * 1024) return file;

    try {
      const img = await blobToImage(file);
      const maxDim = 2600;
      const scale = Math.min(1, maxDim / Math.max(img.width, img.height));
      const outW = Math.max(1, Math.round(img.width * scale));
      const outH = Math.max(1, Math.round(img.height * scale));

      const cnv = document.createElement("canvas");
      cnv.width = outW;
      cnv.height = outH;
      const ctx = cnv.getContext("2d");
      if (!ctx) return file;
      ctx.drawImage(img, 0, 0, outW, outH);

      const blob = await canvasToBlob(cnv, "image/jpeg", 0.95);
      if (!blob) return file;

      const base = (file.name || "ballot").replace(/\.[a-z0-9]+$/i, "");
      return new File([blob], `${base}.jpg`, { type: "image/jpeg" });
    } catch (_) {
      return file;
    }
  };

  const revokePreviewUrls = () => {
    if (originalPreviewUrl) {
      URL.revokeObjectURL(originalPreviewUrl);
      originalPreviewUrl = null;
    }
    if (correctedPreviewUrl) {
      URL.revokeObjectURL(correctedPreviewUrl);
      correctedPreviewUrl = null;
    }
  };

  const applyPreviewSelection = () => {
    if (!camPreview) return;

    const chosenBlob = usingCorrectedPreview && correctedBlob ? correctedBlob : originalBlob;
    if (!chosenBlob) return;

    const filename = usingCorrectedPreview && correctedBlob ? "ballot_corrected.jpg" : "ballot_original.jpg";
    const file = new File([chosenBlob], filename, { type: "image/jpeg" });
    setHiddenFile(file);

    revokePreviewUrls();
    if (originalBlob) originalPreviewUrl = URL.createObjectURL(originalBlob);
    if (correctedBlob) correctedPreviewUrl = URL.createObjectURL(correctedBlob);

    camPreview.src =
      usingCorrectedPreview && correctedBlob
        ? correctedPreviewUrl || ""
        : originalPreviewUrl || correctedPreviewUrl || "";

    if (btnTogglePreviewSource) {
      btnTogglePreviewSource.hidden = !correctedBlob;
      btnTogglePreviewSource.textContent = usingCorrectedPreview
        ? "Foloseste imaginea originala"
        : "Foloseste imaginea corectata";
    }
  };

  const rotatePoint = (pt, cx, cy, angle) => {
    const s = Math.sin(angle);
    const c = Math.cos(angle);
    const x = pt.x - cx;
    const y = pt.y - cy;
    return {
      x: x * c - y * s + cx,
      y: x * s + y * c + cy,
    };
  };

  const buildDeskewBlob = async (sourceCanvas, normalizedPoints) => {
    if (!normalizedPoints) return null;

    const sw = sourceCanvas.width;
    const sh = sourceCanvas.height;
    const keys = ["tl", "tr", "bl", "br"];
    for (const k of keys) {
      if (!normalizedPoints[k]) return null;
    }

    const points = {
      tl: { x: normalizedPoints.tl.x * sw, y: normalizedPoints.tl.y * sh },
      tr: { x: normalizedPoints.tr.x * sw, y: normalizedPoints.tr.y * sh },
      bl: { x: normalizedPoints.bl.x * sw, y: normalizedPoints.bl.y * sh },
      br: { x: normalizedPoints.br.x * sw, y: normalizedPoints.br.y * sh },
    };

    const angle = Math.atan2(points.tr.y - points.tl.y, points.tr.x - points.tl.x);

    const rotCanvas = document.createElement("canvas");
    rotCanvas.width = sw;
    rotCanvas.height = sh;
    const rctx = rotCanvas.getContext("2d");
    if (!rctx) return null;

    const cx = sw / 2;
    const cy = sh / 2;
    rctx.translate(cx, cy);
    rctx.rotate(-angle);
    rctx.drawImage(sourceCanvas, -cx, -cy);
    rctx.setTransform(1, 0, 0, 1, 0, 0);

    const rPts = {
      tl: rotatePoint(points.tl, cx, cy, -angle),
      tr: rotatePoint(points.tr, cx, cy, -angle),
      bl: rotatePoint(points.bl, cx, cy, -angle),
      br: rotatePoint(points.br, cx, cy, -angle),
    };

    let minX = Math.min(rPts.tl.x, rPts.tr.x, rPts.bl.x, rPts.br.x);
    let maxX = Math.max(rPts.tl.x, rPts.tr.x, rPts.bl.x, rPts.br.x);
    let minY = Math.min(rPts.tl.y, rPts.tr.y, rPts.bl.y, rPts.br.y);
    let maxY = Math.max(rPts.tl.y, rPts.tr.y, rPts.bl.y, rPts.br.y);

    const pad = Math.round(Math.min(sw, sh) * 0.035);
    minX = clamp(Math.floor(minX - pad), 0, sw - 1);
    minY = clamp(Math.floor(minY - pad), 0, sh - 1);
    maxX = clamp(Math.ceil(maxX + pad), 1, sw);
    maxY = clamp(Math.ceil(maxY + pad), 1, sh);

    const cw = Math.max(1, maxX - minX);
    const ch = Math.max(1, maxY - minY);

    if (cw < sw * 0.25 || ch < sh * 0.25) return null;

    const cropCanvas = document.createElement("canvas");
    cropCanvas.width = cw;
    cropCanvas.height = ch;
    const cctx = cropCanvas.getContext("2d");
    if (!cctx) return null;

    cctx.drawImage(rotCanvas, minX, minY, cw, ch, 0, 0, cw, ch);
    return canvasToBlob(cropCanvas, "image/jpeg", 0.95);
  };

  const computeGrayStats = (imageData) => {
    const { data, width, height } = imageData;
    const gray = new Uint8Array(width * height);
    let sum = 0;
    let sum2 = 0;

    for (let i = 0, j = 0; i < data.length; i += 4, j += 1) {
      const g = Math.round(data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114);
      gray[j] = g;
      sum += g;
      sum2 += g * g;
    }

    const count = gray.length || 1;
    const mean = sum / count;
    const variance = Math.max(0, sum2 / count - mean * mean);
    return { gray, width, height, mean, std: Math.sqrt(variance) };
  };

  const regionStats = (gray, w, x0, y0, x1, y1, step = 3) => {
    let sum = 0;
    let sum2 = 0;
    let count = 0;

    for (let y = y0; y < y1; y += step) {
      for (let x = x0; x < x1; x += step) {
        const v = gray[y * w + x];
        sum += v;
        sum2 += v * v;
        count += 1;
      }
    }

    if (!count) return { mean: 0, std: 0 };
    const mean = sum / count;
    const variance = Math.max(0, sum2 / count - mean * mean);
    return { mean, std: Math.sqrt(variance) };
  };

  const patchStats = (gray, w, x0, y0, size, step = 2) => {
    let sum = 0;
    let sum2 = 0;
    let count = 0;

    const x1 = x0 + size;
    const y1 = y0 + size;
    for (let y = y0; y < y1; y += step) {
      for (let x = x0; x < x1; x += step) {
        const v = gray[y * w + x];
        sum += v;
        sum2 += v * v;
        count += 1;
      }
    }

    if (!count) return { mean: 0, std: 0 };
    const mean = sum / count;
    const variance = Math.max(0, sum2 / count - mean * mean);
    return { mean, std: Math.sqrt(variance) };
  };

  const computeBlurMetric = (gray, w, h) => {
    let acc = 0;
    let count = 0;
    for (let y = 1; y < h - 1; y += 2) {
      for (let x = 1; x < w - 1; x += 2) {
        const idx = y * w + x;
        const lap = Math.abs(4 * gray[idx] - gray[idx - 1] - gray[idx + 1] - gray[idx - w] - gray[idx + w]);
        acc += lap;
        count += 1;
      }
    }
    return count ? acc / count : 0;
  };

  const buildAdaptiveProfile = (mean, std) => {
    const dpr = window.devicePixelRatio || 1;
    const profile = {
      markerThreshold: dpr >= 2 ? 0.43 : 0.39,
      scoreShift: 0.12,
      scoreScale: 1.05,
      stabilityFrames: dpr >= 2 ? 4 : 3,
      blurWarn: 15,
      blurFail: 9,
      minReadyScore: 58,
    };

    if (mean < 80) {
      profile.markerThreshold -= 0.05;
      profile.stabilityFrames += 1;
      profile.blurWarn = 13;
      profile.blurFail = 7;
      profile.minReadyScore -= 4;
    }

    if (mean > 195) {
      profile.markerThreshold += 0.03;
      profile.minReadyScore += 2;
    }

    if (std < 28) {
      profile.markerThreshold += 0.03;
      profile.minReadyScore += 3;
    }

    return profile;
  };

  const detectCornerMarker = (gray, w, h, corner, profile) => {
    const roiW = Math.floor(w * 0.26);
    const roiH = Math.floor(h * 0.26);

    const maps = {
      tl: { x0: 0, y0: 0, ex: 0.14, ey: 0.14 },
      tr: { x0: w - roiW, y0: 0, ex: 0.86, ey: 0.14 },
      bl: { x0: 0, y0: h - roiH, ex: 0.14, ey: 0.86 },
      br: { x0: w - roiW, y0: h - roiH, ex: 0.86, ey: 0.86 },
    };

    const cfg = maps[corner];
    const x0 = cfg.x0;
    const y0 = cfg.y0;
    const x1 = x0 + roiW;
    const y1 = y0 + roiH;

    const roi = regionStats(gray, w, x0, y0, x1, y1, 3);
    const roiStd = Math.max(roi.std, 6);

    const block = Math.max(8, Math.round(Math.min(w, h) / 44));
    const step = Math.max(3, Math.round(block / 3));

    let best = { score: -999, confidence: 0, x: null, y: null };
    const ex = x0 + roiW * cfg.ex;
    const ey = y0 + roiH * cfg.ey;

    for (let y = y0; y <= y1 - block; y += step) {
      for (let x = x0; x <= x1 - block; x += step) {
        const p = patchStats(gray, w, x, y, block, 2);

        const darkness = (roi.mean - p.mean) / (roiStd + 1e-5);
        const texture = p.std / (roiStd + 1e-5);

        const cx = x + block / 2;
        const cy = y + block / 2;
        const dx = (cx - ex) / roiW;
        const dy = (cy - ey) / roiH;
        const distPenalty = Math.sqrt(dx * dx + dy * dy);

        const score = darkness * 0.62 + texture * 0.38 - distPenalty * 0.35;
        if (score > best.score) {
          const confidence = clamp((score - profile.scoreShift) / profile.scoreScale, 0, 1);
          best = { score, confidence, x: cx, y: cy };
        }
      }
    }

    const found = best.confidence >= profile.markerThreshold;
    return {
      found,
      confidence: Number(best.confidence.toFixed(4)),
      point: best.x != null ? { x: best.x, y: best.y } : null,
    };
  };

  const evaluateLiveQuality = (grayStats, markersFoundCount, profile) => {
    const blur = computeBlurMetric(grayStats.gray, grayStats.width, grayStats.height);
    const brightness = grayStats.mean;

    const reasons = [];
    let status = "ok";

    if (blur < profile.blurFail) {
      reasons.push("very_blurry");
      status = "fail";
    } else if (blur < profile.blurWarn) {
      reasons.push("blurry");
      if (status === "ok") status = "warn";
    }

    if (brightness < 55) {
      reasons.push("too_dark");
      if (status === "ok") status = "warn";
    } else if (brightness > 222) {
      reasons.push("too_bright");
      if (status === "ok") status = "warn";
    }

    if (markersFoundCount < 4) {
      reasons.push("frame_missing");
      if (markersFoundCount <= 2) {
        status = "fail";
      } else if (status === "ok") {
        status = "warn";
      }
    }

    return {
      status,
      reasons,
      metrics: {
        blur_score: Number(blur.toFixed(2)),
        brightness: Number(brightness.toFixed(2)),
      },
    };
  };

  const computeConfidenceScore = ({ corners, foundCount, quality, geometryOk }) => {
    const confAvg =
      (corners.tl.confidence + corners.tr.confidence + corners.bl.confidence + corners.br.confidence) / 4;
    const qualityFactor = quality.status === "ok" ? 1 : quality.status === "warn" ? 0.72 : 0.35;

    let score = 0;
    score += confAvg * 55;
    score += (foundCount / 4) * 25;
    score += qualityFactor * 20;
    score += geometryOk ? 5 : -10;

    if (quality.reasons.includes("very_blurry")) score -= 14;
    if (quality.reasons.includes("frame_missing") && foundCount < 4) score -= 10;

    return Math.round(clamp(score, 0, 100));
  };

  const normalizePoints = (points, w, h) => {
    const out = {};
    for (const key of ["tl", "tr", "bl", "br"]) {
      if (!points[key]) {
        out[key] = null;
      } else {
        out[key] = {
          x: clamp(points[key].x / w, 0, 1),
          y: clamp(points[key].y / h, 0, 1),
        };
      }
    }
    return out;
  };

  const detectLiveFrameClient = () => {
    if (!video || !canvas || !video.videoWidth || !video.videoHeight) return null;

    const targetW = 640;
    const targetH = Math.max(360, Math.round((video.videoHeight / video.videoWidth) * targetW));
    canvas.width = targetW;
    canvas.height = targetH;

    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(video, 0, 0, targetW, targetH);

    const imageData = ctx.getImageData(0, 0, targetW, targetH);
    const grayStats = computeGrayStats(imageData);
    const profile = buildAdaptiveProfile(grayStats.mean, grayStats.std);
    stableTargetFrames = profile.stabilityFrames;

    const corners = {
      tl: detectCornerMarker(grayStats.gray, targetW, targetH, "tl", profile),
      tr: detectCornerMarker(grayStats.gray, targetW, targetH, "tr", profile),
      bl: detectCornerMarker(grayStats.gray, targetW, targetH, "bl", profile),
      br: detectCornerMarker(grayStats.gray, targetW, targetH, "br", profile),
    };

    const foundCount = [corners.tl, corners.tr, corners.bl, corners.br].filter((c) => c.found).length;

    const points = {
      tl: corners.tl.found ? corners.tl.point : null,
      tr: corners.tr.found ? corners.tr.point : null,
      bl: corners.bl.found ? corners.bl.point : null,
      br: corners.br.found ? corners.br.point : null,
    };

    let geometryOk = false;
    if (foundCount === 4) {
      const tl = points.tl;
      const tr = points.tr;
      const bl = points.bl;
      const br = points.br;

      geometryOk =
        tl.x < tr.x &&
        bl.x < br.x &&
        tl.y < bl.y &&
        tr.y < br.y &&
        tr.x - tl.x > targetW * 0.28 &&
        bl.y - tl.y > targetH * 0.28;
    }

    const quality = evaluateLiveQuality(grayStats, foundCount, profile);
    const score = computeConfidenceScore({ corners, foundCount, quality, geometryOk });
    const readyToCapture = foundCount === 4 && geometryOk && quality.status !== "fail" && score >= profile.minReadyScore;

    return {
      corners,
      points,
      foundCount,
      geometryOk,
      quality,
      score,
      readyToCapture,
      markerNorm: normalizePoints(points, targetW, targetH),
      targetW,
      targetH,
      profile,
    };
  };

  const analyzeFile = async (file) => {
    if (!file || !slug) return null;

    const body = new FormData();
    body.append("ballot", file);

    const fetchWithTimeout = async (url, options, timeoutMs) => {
      const controller = new AbortController();
      const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
      try {
        return await fetch(url, { ...options, signal: controller.signal });
      } finally {
        window.clearTimeout(timeoutId);
      }
    };

    try {
      const response = await fetchWithTimeout(`/api/${slug}/scan/validate-image`, {
        method: "POST",
        headers: {
          "X-CSRF-Token": csrf,
        },
        body,
      }, 12000);

      if (!response.ok) {
        const fallback = { status: "warn", reasons: [], metrics: {} };
        renderQuality(fallback);
        return fallback;
      }

      const quality = await response.json();
      renderQuality(quality);
      return quality;
    } catch (_) {
      const fallback = { status: "warn", reasons: [], metrics: {} };
      renderQuality(fallback);
      return fallback;
    }
  };

  const stopLiveProbe = () => {
    if (liveProbeTimer) {
      window.clearInterval(liveProbeTimer);
      liveProbeTimer = null;
    }
    liveProbeInFlight = false;
    stableAlignedFrames = 0;
  };

  const stopCamera = () => {
    stopLiveProbe();
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
    if (startBtn) startBtn.disabled = false;
    if (autoBtn) autoBtn.disabled = true;
    if (captureBtn) captureBtn.disabled = true;
    latestMarkerNorm = null;
    latestMarkerSource = null;
    resetMarkerDots();
  };

  const runLiveProbe = async () => {
    if (liveProbeInFlight || !stream) return;
    if (currentTab !== "camera") return;
    if (camCaptured && !camCaptured.hasAttribute("hidden")) return;
    if (!video?.videoWidth || !video?.videoHeight) return;

    liveProbeInFlight = true;
    try {
      const live = detectLiveFrameClient();
      if (!live) return;

      latestMarkerNorm = live.markerNorm;
      latestMarkerSource = { width: live.targetW, height: live.targetH };
      latestLiveScore = live.score;
      positionMarkerDots(latestMarkerNorm, live.targetW, live.targetH);

      const stateByCorner = {
        tl: live.corners.tl,
        tr: live.corners.tr,
        bl: live.corners.bl,
        br: live.corners.br,
      };

      ["tl", "tr", "bl", "br"].forEach((k) => {
        const c = stateByCorner[k];
        if (c.found) {
          setMarkerDotState(k, "ok");
        } else if (c.confidence > 0.28) {
          setMarkerDotState(k, "warn");
        } else {
          setMarkerDotState(k, "");
        }
      });

      renderQuality(live.quality);

      if (live.readyToCapture) {
        stableAlignedFrames += 1;
        setLiveStatus(
          `Aliniere stabila ${stableAlignedFrames}/${stableTargetFrames} • scor ${live.score}/100`,
          "ok",
        );
      } else {
        stableAlignedFrames = 0;
        if (live.foundCount === 0) {
          setLiveStatus("Caut markerii in cele 4 colturi...", "warn");
        } else {
          setLiveStatus(
            `Markeri detectati ${live.foundCount}/4 • scor ${live.score}/100. Ajusteaza cadrul.`,
            "warn",
          );
        }
      }

      if (autoCaptureEnabled && live.readyToCapture && stableAlignedFrames >= stableTargetFrames) {
        stableAlignedFrames = 0;
        await captureCameraFrame(true);
      }
    } finally {
      liveProbeInFlight = false;
    }
  };

  const startLiveProbe = () => {
    stopLiveProbe();
    runLiveProbe();
    liveProbeTimer = window.setInterval(runLiveProbe, 450);
  };

  const startCamera = async () => {
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: { ideal: "environment" },
          width: { ideal: 1920 },
          height: { ideal: 1080 },
        },
      });
      if (video) video.srcObject = stream;
      if (startBtn) startBtn.disabled = true;
      if (autoBtn) autoBtn.disabled = false;
      if (captureBtn) captureBtn.disabled = false;

      if (captureModeInput) captureModeInput.value = "live_preview";
      setStep(2, [1]);
      setLiveStatus("Camera activa. Tineti telefonul stabil pentru detectie live.");
      resetMarkerDots();
      startLiveProbe();
    } catch (err) {
      window.alert(`Camera nu a putut fi pornita: ${err.message}`);
    }
  };

  const captureCameraFrame = async (isAuto = false) => {
    if (captureInProgress) return;
    if (!video || !video.videoWidth || !video.videoHeight || !canvas) return;

    captureInProgress = true;
    try {
      const fullCanvas = document.createElement("canvas");
      fullCanvas.width = video.videoWidth;
      fullCanvas.height = video.videoHeight;
      const fctx = fullCanvas.getContext("2d");
      if (!fctx) return;

      fctx.drawImage(video, 0, 0, fullCanvas.width, fullCanvas.height);

      const raw = await canvasToBlob(fullCanvas, "image/jpeg", 0.95);
      if (!raw) return;

      originalBlob = raw;
      correctedBlob = await buildDeskewBlob(fullCanvas, latestMarkerNorm);
      // Keep the original full frame as default to preserve QR/marker content.
      usingCorrectedPreview = false;

      applyPreviewSelection();

      camLive?.setAttribute("hidden", "hidden");
      camCaptured?.removeAttribute("hidden");
      captureReview?.removeAttribute("hidden");

      stopCamera();

      const mode = isAuto ? "auto_live" : "manual_live";
      if (captureModeInput) captureModeInput.value = mode;

      let score = latestLiveScore || 72;
      const selectedBlob = usingCorrectedPreview && correctedBlob ? correctedBlob : originalBlob;
      const selectedFile = new File([selectedBlob], "ballot.jpg", { type: "image/jpeg" });

      const quality = await analyzeFile(selectedFile);
      if (quality) {
        if (quality.status === "ok") score = clamp(score + 4, 0, 100);
        if (quality.status === "warn") score = clamp(score - 3, 0, 100);
        if (quality.status === "fail") score = clamp(score - 18, 0, 100);
      }

      setCaptureConfidence(score);
      setStep(3, [1, 2]);
      setLiveStatus(
        isAuto
          ? `Captura automata realizata • scor ${Math.round(score)}/100`
          : `Captura manuala realizata • scor ${Math.round(score)}/100`,
        score >= lowConfidenceThreshold ? "ok" : "warn",
      );
    } finally {
      captureInProgress = false;
    }
  };

  const switchTab = (tabName) => {
    currentTab = tabName;
    tabButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabName));
    Object.entries(tabPanels).forEach(([name, panel]) => {
      panel?.classList.toggle("active", name === tabName);
    });

    if (tabName !== "camera") {
      stopCamera();
      setLiveStatus("Mod upload activ.");
    }
  };

  const showCameraWarning = (text) => {
    if (!cameraWarning) return;
    cameraWarning.hidden = false;
    cameraWarning.textContent = text;
  };

  // Camera handlers
  startBtn?.addEventListener("click", startCamera);
  captureBtn?.addEventListener("click", () => captureCameraFrame(false));

  autoBtn?.addEventListener("click", () => {
    autoCaptureEnabled = !autoCaptureEnabled;
    autoBtn.textContent = `Auto-capture: ${autoCaptureEnabled ? "ON" : "OFF"}`;
    setLiveStatus(
      autoCaptureEnabled
        ? "Auto-capture activ. Captura se face cand markerii sunt stabili."
        : "Auto-capture dezactivat. Foloseste capturarea manuala.",
      autoCaptureEnabled ? "ok" : "warn",
    );
  });

  btnTogglePreviewSource?.addEventListener("click", async () => {
    if (!correctedBlob) return;
    usingCorrectedPreview = !usingCorrectedPreview;
    applyPreviewSelection();

    const blob = usingCorrectedPreview && correctedBlob ? correctedBlob : originalBlob;
    if (!blob) return;
    const file = new File([blob], "ballot.jpg", { type: "image/jpeg" });
    const quality = await analyzeFile(file);

    let score = Number(captureConfidenceInput?.value || latestLiveScore || 72);
    if (quality) {
      if (quality.status === "ok") score = clamp(score + 2, 0, 100);
      if (quality.status === "warn") score = clamp(score - 2, 0, 100);
      if (quality.status === "fail") score = clamp(score - 12, 0, 100);
    }
    setCaptureConfidence(score);
  });

  retakeBtn?.addEventListener("click", async () => {
    camCaptured?.setAttribute("hidden", "hidden");
    camLive?.removeAttribute("hidden");
    resetCaptureMeta();
    if (btnScanCam) btnScanCam.disabled = true;
    revokePreviewUrls();
    originalBlob = null;
    correctedBlob = null;
    usingCorrectedPreview = false;
    setLiveStatus("Reluare captura. Reincadreaza buletinul.");
    await startCamera();
  });

  overrideCheckbox?.addEventListener("change", () => {
    if (operatorOverrideInput) operatorOverrideInput.value = overrideCheckbox.checked ? "1" : "0";
    updateCameraSubmitEnabled();
  });

  // Upload handlers
  const onFileSelected = async (file) => {
    if (!file) return;

    const preparedFile = await toJpegFile(file);
    selectedBallotFile = preparedFile;
    setInputFile(hiddenInput, preparedFile);

    if (uploadPreview) uploadPreview.src = URL.createObjectURL(preparedFile);
    uploadPreviewWrap?.removeAttribute("hidden");

    if (captureModeInput) captureModeInput.value = "upload_file";
    if (operatorOverrideInput) operatorOverrideInput.value = "0";

    const quality = await analyzeFile(preparedFile);
    const score = scoreFromQuality(quality);
    if (captureConfidenceInput) captureConfidenceInput.value = String(score);

    setUploadSubmitEnabled(true);
    setStep(3, [1, 2]);
    if (quality?.status === "fail") {
      setLiveStatus(`Calitate slaba detectata • scor estimat ${score}/100. Reincadreaza pentru procesare rapida.`, "warn");
    } else {
      setLiveStatus(`Upload pregatit • scor estimat ${score}/100`);
    }
  };

  uploadInput?.addEventListener("change", async () => {
    const file = uploadInput.files?.[0];
    await onFileSelected(file);
  });

  uploadDrop?.addEventListener("dragover", (ev) => {
    ev.preventDefault();
    uploadDrop.classList.add("drag-over");
  });

  uploadDrop?.addEventListener("dragleave", () => uploadDrop.classList.remove("drag-over"));

  uploadDrop?.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    uploadDrop.classList.remove("drag-over");

    const file = ev.dataTransfer?.files?.[0];
    if (!file) return;

    if (uploadInput) {
      setInputFile(uploadInput, file);
    }

    await onFileSelected(file);
  });

  // Tab handlers
  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Fallback when camera API unavailable
  const cameraApiAvailable = Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const secureContextAvailable = Boolean(window.isSecureContext);
  if (!cameraApiAvailable || !secureContextAvailable) {
    const cameraTabBtn = tabButtons.find((btn) => btn.dataset.tab === "camera");
    cameraTabBtn?.setAttribute("hidden", "hidden");
    switchTab("upload");

    if (!secureContextAvailable) {
      showCameraWarning(
        "Live preview cu detectie automata necesita HTTPS. Acceseaza aplicatia pe un URL securizat (https://...) pentru camera live."
      );
    } else {
      showCameraWarning(
        "Browserul nu expune API-ul de camera live pe acest dispozitiv. Aplicatia foloseste modul upload/foto."
      );
    }
  }

  // Submit state
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    setStep(4, [1, 2, 3]);
    btnScanCam && (btnScanCam.disabled = true);
    btnScanUpload && (btnScanUpload.disabled = true);

    if (currentTab === "camera") {
      spinnerCam?.style.setProperty("display", "flex");
    } else {
      spinnerUpload?.style.setProperty("display", "flex");
    }

    const fileFromInputs = hiddenInput?.files?.[0] || uploadInput?.files?.[0] || null;
    const ballotFile = selectedBallotFile || fileFromInputs;
    if (!ballotFile) {
      window.alert("Selectati sau capturati un buletin inainte de trimitere.");
      spinnerCam?.style.setProperty("display", "none");
      spinnerUpload?.style.setProperty("display", "none");
      btnScanCam && (btnScanCam.disabled = false);
      btnScanUpload && (btnScanUpload.disabled = false);
      return;
    }

    const body = new FormData(form);
    body.set("ballot", ballotFile, ballotFile.name || "ballot.jpg");

    if (qualityStatus === "fail") {
      const proceed = window.confirm(
        "Calitatea imaginii este slaba si scanarea poate esua. Vrei sa trimiti totusi?"
      );
      if (!proceed) {
        spinnerCam?.style.setProperty("display", "none");
        spinnerUpload?.style.setProperty("display", "none");
        updateCameraSubmitEnabled();
        setUploadSubmitEnabled(true);
        setStep(3, [1, 2]);
        return;
      }
    }

    try {
      const controller = new AbortController();
      const timeoutId = window.setTimeout(() => controller.abort(), 90000);
      let response;
      try {
        response = await fetch(form.action, {
          method: "POST",
          body,
          credentials: "same-origin",
          redirect: "follow",
          signal: controller.signal,
        });
      } finally {
        window.clearTimeout(timeoutId);
      }

      if (response.redirected && response.url) {
        window.location.assign(response.url);
        return;
      }

      const html = await response.text();
      document.open();
      document.write(html);
      document.close();
    } catch (err) {
      spinnerCam?.style.setProperty("display", "none");
      spinnerUpload?.style.setProperty("display", "none");
      updateCameraSubmitEnabled();
      setUploadSubmitEnabled(true);
      setStep(3, [1, 2]);
      if (err?.name === "AbortError") {
        window.alert("Procesarea a durat prea mult pe server. Reincarca fotografia si incearca din nou.");
      } else {
        window.alert("Trimiterea imaginii a esuat. Verifica conexiunea si incearca din nou.");
      }
    }
  });

  // Initial UI state
  resetCaptureMeta();
  setUploadSubmitEnabled(false);
  if (btnScanCam) btnScanCam.disabled = true;
  setStep(1, []);
  setLiveStatus("Porniti camera pentru detectia markerilor.");
  resetMarkerDots();
  if (autoBtn) {
    autoBtn.textContent = "Auto-capture: ON";
    autoBtn.disabled = true;
  }

  window.addEventListener("resize", () => {
    if (latestMarkerNorm && latestMarkerSource) {
      positionMarkerDots(latestMarkerNorm, latestMarkerSource.width, latestMarkerSource.height);
    }
  });

  window.addEventListener("beforeunload", () => {
    stopCamera();
    revokePreviewUrls();
  });
});
