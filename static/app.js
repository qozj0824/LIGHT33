"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  allskyToken: null,
  allskyMetadata: null,
  allskyAssessment: null,
  target: null,
  referenceTarget: null,
  profiles: [],
  analyzing: false,
  creatingProfile: false,
  allskyInspectSequence: 0,
  allskyInspectController: null,
  allskyInspecting: false,
  allskyInspectFailed: false,
  referenceScopeMetadata: null,
  referenceScopeCaptureTimeUtc: null,
  referenceScopeInspectSequence: 0,
  referenceScopeInspectController: null,
  referenceAllskyMetadata: null,
  referenceAllskyInspectSequence: 0,
  referenceAllskyInspectController: null,
  objectUrls: new Map(),
  stellariumAutoTimer: null,
  stellariumSyncInFlight: false,
  stellariumFresh: false,
  serverInstanceId: null,
  analysisError: null,
};

const ARTIFACT_INFO = {
  sky_polar_map: ["방향별 하늘 배경", ""],
  allsky_coordinate_overlay: ["전천 좌표", ""],
  sky_relative_map: ["상대 배경", ""],
  sky_reliability: ["셀 신뢰도", ""],
  sky_altitude_profiles: ["고도별 프로파일", ""],
  sky_map: ["방위각–고도 ADU", ""],
  sky_distribution: ["배경 분포", ""],
  allsky_preview: ["전천 영상", ""],
  exposure_snr_curve: ["노출시간–SNR", ""],
};

function valueOrNull(id) {
  const raw = $(id).value.trim();
  return raw === "" ? null : Number(raw);
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return Number(value).toLocaleString("ko-KR", { maximumFractionDigits: digits });
}

function formatSeconds(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const seconds = Number(value);
  if (seconds < 60) return `${formatNumber(seconds, seconds < 10 ? 1 : 0)}초`;
  if (seconds < 3600) return `${formatNumber(seconds / 60, 1)}분`;
  return `${formatNumber(seconds / 3600, 2)}시간`;
}

function formatSubExposureSeconds(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const seconds = Number(value);
  const digits = seconds < 10 ? 1 : (Number.isInteger(seconds) ? 0 : 1);
  return `${formatNumber(seconds, digits)}초`;
}

function assessmentLabel(assessment) {
  return {
    ready: "자동 판정 완료",
    usable_with_fallbacks: "자동 보정 적용",
    needs_input: "추가 입력 필요",
  }[assessment?.status] || "";
}

function assessmentSummary(assessment) {
  if (!assessment) return "";
  const items = [
    ...(assessment.required_actions || []),
    ...(assessment.automatic_recoveries || []),
    ...(assessment.warnings || []),
  ];
  return items.length ? items.slice(0, 3).join(" · ") : "";
}

function escapeText(value) { return String(value ?? ""); }

const PROFILE_SNAPSHOT_KEY = "noxis.profileSnapshots.v1";

function loadProfileSnapshots() {
  try {
    const raw = JSON.parse(localStorage.getItem(PROFILE_SNAPSHOT_KEY) || "{}");
    return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  } catch { return {}; }
}

function saveProfileSnapshot(profile) {
  if (!profile?.profile_id) return;
  try {
    const snapshots = loadProfileSnapshots();
    snapshots[profile.profile_id] = profile;
    localStorage.setItem(PROFILE_SNAPSHOT_KEY, JSON.stringify(snapshots));
  } catch { /* localStorage may be unavailable; server profile still works. */ }
}

async function compactPreviewDataUrl(url) {
  if (!url || String(url).startsWith("data:")) return url || null;
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) return null;
    const bitmap = await createImageBitmap(await response.blob());
    const scale = Math.min(1, 640 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    return canvas.toDataURL("image/jpeg", 0.72);
  } catch { return null; }
}

async function cacheProfilePreviewData(profile) {
  if (!profile?.profile_id) return profile;
  const existing = getProfileSnapshot(profile.profile_id) || {};
  const enriched = { ...existing, ...profile };
  if (!enriched.scope_preview_data_url && profile.scope_preview_url) {
    enriched.scope_preview_data_url = await compactPreviewDataUrl(profile.scope_preview_url);
  }
  if (!enriched.allsky_preview_data_url && profile.allsky_preview_url) {
    enriched.allsky_preview_data_url = await compactPreviewDataUrl(profile.allsky_preview_url);
  }
  return enriched;
}

function removeProfileSnapshot(profileId) {
  try {
    const snapshots = loadProfileSnapshots();
    delete snapshots[profileId];
    localStorage.setItem(PROFILE_SNAPSHOT_KEY, JSON.stringify(snapshots));
  } catch { }
}

function getProfileSnapshot(profileId) {
  return loadProfileSnapshots()[profileId] || null;
}

async function cacheServerProfileSnapshots(profiles) {
  await Promise.all((profiles || []).map(async (profile) => {
    if (!profile?.profile_id) return;
    try {
      const response = await fetch(`/api/equipment/profiles/${encodeURIComponent(profile.profile_id)}`, { cache: "no-store" });
      const payload = await readJsonResponse(response);
      if (response.ok && payload?.profile_id) {
        const cached = await cacheProfilePreviewData({ ...payload, ...profile });
        saveProfileSnapshot(cached);
      }
    } catch { }
  }));
}

function noteServerInstance(instanceId) {
  if (!instanceId) return;
  if (state.serverInstanceId && state.serverInstanceId !== instanceId) {
    // Render restarted: preview tokens are ephemeral, but the selected browser file and
    // cached equipment profile remain usable and are sent again automatically.
    state.allskyToken = null;
  }
  state.serverInstanceId = instanceId;
}

function normalizeStellariumUrl(rawValue) {
  let raw = String(rawValue || "").trim();
  if (!raw) raw = "http://127.0.0.1:8090";
  if (!/^https?:\/\//i.test(raw)) raw = `http://${raw}`;
  const url = new URL(raw);
  if (!/^https?:$/.test(url.protocol)) throw new Error("주소 확인");
  return `${url.protocol}//${url.host}`;
}

function encodeBasicPassword(password) {
  const bytes = new TextEncoder().encode(`:${password}`);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary);
}

function stellariumAddressSpace(base) {
  try {
    const host = new URL(base).hostname.toLowerCase();
    if (host === "localhost" || host === "::1" || /^127\./.test(host)) return "loopback";
    return "local";
  } catch {
    return "loopback";
  }
}

async function stellariumRequest(endpoint, options = {}) {
  const base = normalizeStellariumUrl($("stellariumUrl").value);
  const password = $("stellariumPassword")?.value || "";
  const headers = {
    Accept: "application/json,text/plain;q=0.9,*/*;q=0.5",
    ...(options.headers || {}),
  };
  if (password) headers.Authorization = `Basic ${encodeBasicPassword(password)}`;
  const response = await fetch(`${base}${endpoint}`, {
    ...options,
    mode: "cors",
    credentials: "omit",
    cache: "no-store",
    targetAddressSpace: stellariumAddressSpace(base),
    headers,
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`HTTP ${response.status}${text ? ` · ${text.slice(0, 120)}` : ""}`);
  if (!text.trim()) return {};
  try { return JSON.parse(text); } catch { return { raw_text: text }; }
}

function stellariumFailureMessage(error) {
  const message = String(error?.message || "");
  if (error?.source === "noxis") return "NØXIS 서버 재연결 중";
  if (/401|403/.test(message)) return "Remote Control 암호 확인";
  if (/permission|denied|address space|local network/i.test(message)) return "브라우저의 로컬 네트워크 권한 허용 필요";
  if (/Failed to fetch|NetworkError|Load failed|fetch/i.test(message)) return "Stellarium Remote Control/CORS 확인";
  return message || "연결 실패";
}

function setStellariumIndicator(stateName) {
  const dot = $("stellariumDot");
  if (!dot) return;
  dot.className = `connection-dot ${stateName || ""}`.trim();
}

async function normalizeStellariumPayload(info, status) {
  let response;
  try {
    response = await fetch("/api/stellarium/normalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ info, status }),
    });
  } catch (cause) {
    const error = new Error("NØXIS 서버에 연결할 수 없습니다.");
    error.source = "noxis";
    error.cause = cause;
    throw error;
  }
  const payload = await readJsonResponse(response);
  if (!response.ok) {
    const error = new Error(payload.detail || `HTTP ${response.status}`);
    if (response.status >= 500 || payload.non_json) error.source = "noxis";
    throw error;
  }
  noteServerInstance(payload.server_instance_id);
  return payload;
}

function dateToJulianDay(iso) {
  const milliseconds = Date.parse(iso);
  if (!Number.isFinite(milliseconds)) throw new Error("촬영시각 확인");
  return milliseconds / 86400000 + 2440587.5;
}

function renderMetadata(metadata) {
  if (!metadata) return "메타데이터 없음";
  const exposure = metadata.extra?.exposure_provenance || {};
  const exposureConfidence = { high: "헤더 확실", medium: "헤더 유도", low: "헤더 충돌" }[exposure.confidence];
  const exposureKey = exposure.selected_key ? String(exposure.selected_key) : null;
  const items = [
    `${metadata.width} × ${metadata.height}`,
    metadata.source_type?.toUpperCase(),
    Number(metadata.exposure_sec) > 0 ? `노출 ${formatSubExposureSeconds(metadata.exposure_sec)}` : "노출시간 미확인",
    exposureKey,
    exposureConfidence,
    metadata.camera || null,
  ].filter(Boolean);
  return items.join(" · ");
}

function clearObjectUrl(key) {
  const current = state.objectUrls.get(key);
  if (current) URL.revokeObjectURL(current);
  state.objectUrls.delete(key);
}

function showLocalPreview(file, imageId, key, placeholderId = null) {
  clearObjectUrl(key);
  const image = $(imageId);
  if (!image) return;
  image.removeAttribute("src");
  if (placeholderId && $(placeholderId)) $(placeholderId).classList.remove("hidden");
  if (!file) return;
  const url = URL.createObjectURL(file);
  state.objectUrls.set(key, url);
  image.onload = () => { if (placeholderId && $(placeholderId)) $(placeholderId).classList.add("hidden"); };
  image.onerror = () => {
    image.removeAttribute("src");
    if (placeholderId && $(placeholderId)) $(placeholderId).classList.remove("hidden");
  };
  image.src = url;
}

function showServerPreview(url, imageId, placeholderId = null) {
  if (!url) return;
  const image = $(imageId);
  if (!image) return;
  image.onload = () => { if (placeholderId && $(placeholderId)) $(placeholderId).classList.add("hidden"); };
  image.src = url;
}

async function readJsonResponse(response) {
  const text = await response.text();
  if (!text) return {};
  try { return JSON.parse(text); } catch {
    const trimmed = text.trim();
    if (/^<!doctype|^<html/i.test(trimmed)) {
      return { detail: `NØXIS 서버가 임시 오류 페이지를 반환했습니다 (HTTP ${response.status}). 잠시 후 자동 재연결합니다.`, non_json: true };
    }
    return { detail: text.slice(0, 300), non_json: true };
  }
}

async function inspectAllsky(file) {
  state.allskyInspectSequence += 1;
  const sequence = state.allskyInspectSequence;
  if (state.allskyInspectController) state.allskyInspectController.abort();
  state.allskyInspectController = new AbortController();
  state.allskyToken = null;
  state.allskyMetadata = null;
  state.allskyAssessment = null;
  state.allskyInspecting = Boolean(file);
  state.allskyInspectFailed = false;
  showLocalPreview(file, "allskyPreview", "main-allsky", "allskyPreviewPlaceholder");
  if (!file) {
    $("allskyName").textContent = "선택 안 됨";
    $("allskyPreviewStatus").textContent = "";
    $("allskyMetadata").textContent = "";
    state.allskyInspectController = null;
    state.allskyInspecting = false;
    updateReadyState();
    return;
  }
  $("allskyName").textContent = file.name;
  $("allskyPreviewStatus").textContent = "헤더·미리보기 읽는 중";
  $("allskyMetadata").textContent = `${file.name} · 서버에서 FITS 헤더를 확인하고 있습니다.`;
  updateReadyState();
  const form = new FormData();
  form.append("file", file);
  form.append("role", "allsky");
  try {
    const response = await fetch("/api/inspect", {
      method: "POST", body: form, signal: state.allskyInspectController.signal,
    });
    const payload = await readJsonResponse(response);
    if (sequence !== state.allskyInspectSequence) return;
    if (!response.ok) throw new Error(payload.detail || "전천 영상 읽기 실패");
    state.allskyToken = payload.upload_token || null;
    state.allskyMetadata = payload.metadata || null;
    state.allskyAssessment = payload.assessment || null;
    state.allskyInspectFailed = false;
    showServerPreview(payload.preview_url, "allskyPreview", "allskyPreviewPlaceholder");
    $("allskyPreviewStatus").textContent = assessmentLabel(payload.assessment);
    const assessmentText = assessmentSummary(payload.assessment);
    $("allskyMetadata").textContent = [renderMetadata(payload.metadata), assessmentText].filter(Boolean).join(" · ");
    if (payload.metadata?.exposure_sec && $("allskyExposure").value === "") {
      $("allskyExposure").placeholder = `헤더: ${payload.metadata.exposure_sec}s`;
    }
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.allskyInspectSequence) return;
    // Keep the browser preview usable. The original file is retried during analysis.
    state.allskyInspectFailed = true;
    $("allskyPreviewStatus").textContent = "검사 재시도 가능";
    $("allskyMetadata").textContent = `${file?.name || ""} · 빠른 검사에 실패했지만 분석 시 원본을 다시 읽습니다.`;
  } finally {
    if (sequence === state.allskyInspectSequence) {
      state.allskyInspectController = null;
      state.allskyInspecting = false;
      updateReadyState();
    }
  }
}


function formatCaptureMetadata(metadata, captureTimeUtc) {
  if (!metadata) return "메타데이터 없음";
  const parts = [renderMetadata(metadata)];
  if (metadata.date_obs) parts.push(`원본 촬영시각 ${metadata.date_obs}`);
  if (captureTimeUtc) parts.push(`UTC ${captureTimeUtc}`);
  else if (metadata.date_obs) parts.push("시간대 미확정 — 자동 Stellarium 시각 설정 불가");
  const wcsRa = metadata.extra?.wcs_center_ra_deg;
  const wcsDec = metadata.extra?.wcs_center_dec_deg;
  if (Number.isFinite(Number(wcsRa)) && Number.isFinite(Number(wcsDec))) {
    parts.push(`WCS 중심 RA ${formatNumber(wcsRa, 4)}° / Dec ${formatNumber(wcsDec, 4)}°`);
  }
  return parts.join(" · ");
}

async function inspectReferenceScope(file) {
  state.referenceScopeInspectSequence += 1;
  const sequence = state.referenceScopeInspectSequence;
  if (state.referenceScopeInspectController) state.referenceScopeInspectController.abort();
  state.referenceScopeInspectController = new AbortController();
  state.referenceScopeMetadata = null;
  state.referenceScopeCaptureTimeUtc = null;
  state.referenceTarget = null;
  renderTargetCard(null, true);
  $("profileReferenceMag").value = "";
  $("profileReferenceSize").value = "";
  $("setStellariumReferenceTime").disabled = true;
  showLocalPreview(file, "profileScopePreview", "profile-scope", "profileScopePreviewPlaceholder");
  if (!file) {
    $("profileScopeName").textContent = "선택 안 됨";
    $("profileScopeMetadata").textContent = "";
    $("profileScopePreviewStatus").textContent = "";
    $("profileTimeStatus").textContent = "";
    state.referenceScopeInspectController = null;
    return;
  }
  $("profileScopeName").textContent = file.name;
  $("profileScopePreviewStatus").textContent = "읽는 중";
  const form = new FormData();
  form.append("file", file);
  form.append("role", "scope");
  try {
    const response = await fetch("/api/inspect", { method: "POST", body: form, signal: state.referenceScopeInspectController.signal });
    const payload = await readJsonResponse(response);
    if (sequence !== state.referenceScopeInspectSequence) return;
    if (!response.ok) throw new Error(payload.detail || "기준 영상 확인 실패");
    state.referenceScopeMetadata = payload.metadata;
    state.referenceScopeCaptureTimeUtc = payload.capture_time_utc || null;
    showServerPreview(payload.preview_url, "profileScopePreview", "profileScopePreviewPlaceholder");
    $("profileScopePreviewStatus").textContent = payload.preview_warning ? "미리보기 제한" : assessmentLabel(payload.assessment);
    $("profileScopeMetadata").textContent = [
      formatCaptureMetadata(payload.metadata, payload.capture_time_utc),
      assessmentSummary(payload.assessment),
    ].filter(Boolean).join(" · ");
    $("setStellariumReferenceTime").disabled = !payload.capture_time_utc;
    if (payload.metadata?.exposure_sec && $("profileScopeExposure").value === "") {
      $("profileScopeExposure").placeholder = `헤더: ${payload.metadata.exposure_sec}s`;
    }
    if (!$("cameraName").value.trim() && payload.metadata?.camera) $("cameraName").value = payload.metadata.camera;
    if (!$("captureGainSetting").value.trim() && payload.metadata?.gain_setting) $("captureGainSetting").value = `Gain/ISO ${payload.metadata.gain_setting}`;
    $("profileTimeStatus").textContent = payload.capture_time_utc ? "촬영시각 확인" : "";
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.referenceScopeInspectSequence) return;
    $("profileScopePreviewStatus").textContent = "";
    $("profileScopeMetadata").textContent = file?.name || "";
    $("profileTimeStatus").textContent = "";
  } finally {
    if (sequence === state.referenceScopeInspectSequence) state.referenceScopeInspectController = null;
  }
}

async function inspectReferenceAllsky(file) {
  state.referenceAllskyInspectSequence += 1;
  const sequence = state.referenceAllskyInspectSequence;
  if (state.referenceAllskyInspectController) state.referenceAllskyInspectController.abort();
  state.referenceAllskyInspectController = new AbortController();
  state.referenceAllskyMetadata = null;
  showLocalPreview(file, "profileAllskyPreview", "profile-allsky", "profileAllskyPreviewPlaceholder");
  if (!file) {
    $("profileAllskyName").textContent = "선택 안 됨";
    $("profileAllskyMetadata").textContent = "";
    $("profileAllskyPreviewStatus").textContent = "";
    state.referenceAllskyInspectController = null;
    return;
  }
  $("profileAllskyName").textContent = file.name;
  $("profileAllskyPreviewStatus").textContent = "읽는 중";
  const form = new FormData();
  form.append("file", file);
  form.append("role", "allsky");
  try {
    const response = await fetch("/api/inspect", { method: "POST", body: form, signal: state.referenceAllskyInspectController.signal });
    const payload = await readJsonResponse(response);
    if (sequence !== state.referenceAllskyInspectSequence) return;
    if (!response.ok) throw new Error(payload.detail || "전천 기준 영상 확인 실패");
    state.referenceAllskyMetadata = payload.metadata || null;
    showServerPreview(payload.preview_url, "profileAllskyPreview", "profileAllskyPreviewPlaceholder");
    $("profileAllskyPreviewStatus").textContent = payload.preview_warning ? "미리보기 제한" : assessmentLabel(payload.assessment);
    $("profileAllskyMetadata").textContent = [
      renderMetadata(payload.metadata),
      assessmentSummary(payload.assessment),
    ].filter(Boolean).join(" · ");
    if (payload.metadata?.exposure_sec && $("profileAllskyExposure").value === "") {
      $("profileAllskyExposure").placeholder = `헤더: ${payload.metadata.exposure_sec}s`;
    }
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.referenceAllskyInspectSequence) return;
    $("profileAllskyPreviewStatus").textContent = "";
    $("profileAllskyMetadata").textContent = file?.name || "";
  } finally {
    if (sequence === state.referenceAllskyInspectSequence) state.referenceAllskyInspectController = null;
  }
}


async function setStellariumTime(observationTimeUtc, pause = true, label = "기준 시각") {
  $("profileTimeStatus").textContent = `${label} 적용 중`;
  try {
    const jday = dateToJulianDay(observationTimeUtc);
    const body = new URLSearchParams({
      time: jday.toFixed(10),
      timerate: pause ? "0" : String(1 / 86400),
    });
    await stellariumRequest("/api/main/time", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      body,
    });
    $("profileTimeStatus").textContent = pause ? "기준 시각 적용됨" : "현재 시각 적용됨";
    $("stellariumStatus").textContent = "연결됨";
    $("stellariumStatus").className = "status-text ok";
  } catch (error) {
    $("profileTimeStatus").textContent = `실패 · ${stellariumFailureMessage(error)}`;
  }
}

async function setStellariumReferenceTime() {
  if (!state.referenceScopeCaptureTimeUtc) {
    $("profileTimeStatus").textContent = "촬영시각 없음";
    return;
  }
  await setStellariumTime(state.referenceScopeCaptureTimeUtc, true, "기준 촬영시각");
}

async function setStellariumNow() {
  await setStellariumTime(new Date().toISOString(), false, "현재 시각");
}

function targetSummary(target) {
  const parts = [];
  if (target.object_type) parts.push(target.object_type);
  if (Number.isFinite(Number(target.vmag))) parts.push(`V ${formatNumber(target.vmag, 2)} mag`);
  if (Number.isFinite(Number(target.size_deg))) parts.push(`각크기 ${formatNumber(target.size_deg, 3)}°`);
  if (Number.isFinite(Number(target.alt_deg))) parts.push(`고도 ${formatNumber(target.alt_deg, 2)}°`);
  if (Number.isFinite(Number(target.az_deg))) parts.push(`방위각 ${formatNumber(target.az_deg, 2)}°`);
  return parts.join(" · ");
}

function renderTargetCard(target, reference = false) {
  const card = reference ? $("referenceTargetCard") : $("targetCard");
  if (!target) {
    card.className = "target-card empty";
    card.textContent = reference ? "선택 안 됨" : "Stellarium에서 천체를 선택하세요";
    return;
  }
  card.className = "target-card";
  card.textContent = `${target.name || "선택 천체"} · ${targetSummary(target)}`;
  if (!reference) updateTargetAltitudeStatus();
}

function updateTargetAltitudeStatus() {
  const box = $("targetAltitudeStatus");
  const target = state.target;
  if (!target || !Number.isFinite(Number(target.alt_deg))) {
    box.className = "safety-notice neutral";
    box.querySelector("span").textContent = "대상 선택 필요";
    return;
  }
  const alt = Number(target.alt_deg);
  const minAlt = Number($("minimumSkyAltitude").value || 15);
  if (alt < 0) {
    box.className = "safety-notice danger";
    box.querySelector("span").textContent = `${alt.toFixed(2)}° · 지평선 아래`;
  } else if (alt < minAlt) {
    box.className = "safety-notice warning";
    box.querySelector("span").textContent = `${alt.toFixed(2)}° · 최저 ${minAlt.toFixed(1)}° 미만`;
  } else {
    box.className = "safety-notice good";
    box.querySelector("span").textContent = `${alt.toFixed(2)}° · 분석 가능`;
  }
  updateReadyState();
}

async function stellariumPing({ quiet = false } = {}) {
  const status = $("stellariumStatus");
  if (!quiet) { status.className = "status-text"; status.textContent = "연결 중"; }
  setStellariumIndicator("pending");
  try {
    await stellariumRequest("/api/main/status");
    status.textContent = "연결됨";
    status.className = "status-text ok";
    setStellariumIndicator("ok");
    return true;
  } catch (error) {
    state.stellariumFresh = false;
    status.textContent = quiet ? "재연결 중" : stellariumFailureMessage(error);
    status.className = "status-text error";
    setStellariumIndicator("error");
    updateReadyState();
    return false;
  }
}

async function importStellariumTarget(reference = false, { quiet = false } = {}) {
  const statusText = $("stellariumStatus");
  statusText.className = "status-text";
  if (!quiet) statusText.textContent = "동기화 중";
  setStellariumIndicator("pending");
  try {
    const status = await stellariumRequest("/api/main/status");
    let info = {};
    try { info = await stellariumRequest("/api/objects/info?format=json"); } catch { info = {}; }
    const payload = await normalizeStellariumPayload(info, status);
    const target = {
      name: payload.name, object_type: payload.object_type, target_mode: payload.target_mode,
      vmag: payload.vmag, vmage: payload.vmage, size_deg: payload.size_deg,
      ra_deg: payload.ra_deg, dec_deg: payload.dec_deg, alt_deg: payload.alt_deg, az_deg: payload.az_deg,
      location: payload.location || null, time: payload.time || null,
    };
    state.stellariumFresh = true;
    if (reference) {
      state.referenceTarget = target;
      renderTargetCard(target, true);
      statusText.textContent = `기준 · ${target.name}`;
    } else {
      state.target = target;
      renderTargetCard(target, false);
      statusText.textContent = "연결됨";
    }
    statusText.className = "status-text ok";
    setStellariumIndicator("ok");
    updateReadyState();
    return target;
  } catch (error) {
    if (!reference) state.stellariumFresh = false;
    const baseMessage = stellariumFailureMessage(error);
    statusText.textContent = (!reference && state.target) ? `${baseMessage} · 마지막 천체 유지` : baseMessage;
    statusText.className = "status-text error";
    setStellariumIndicator("error");
    // Do not erase the last valid target on a temporary localhost/Render interruption.
    // Analysis stays disabled until a fresh sync succeeds.
    updateReadyState();
    return null;
  }
}


function appendFiles(form, field, input) {
  [...input.files].forEach((file) => form.append(field, file));
}

function addMaybe(form, key, value) {
  if (value !== null && value !== undefined && value !== "" && !(typeof value === "number" && !Number.isFinite(value))) {
    form.append(key, String(value));
  }
}

async function createEquipmentProfile() {
  if (state.creatingProfile) return;
  const scope = $("profileScopeFile").files[0];
  if (!scope) {
    $("profileCreateStatus").textContent = "망원경 기준 영상을 선택하세요.";
    return;
  }
  const form = new FormData();
  form.append("scope", scope);
  const refAllsky = $("profileAllskyFile").files[0];
  if (refAllsky) form.append("reference_allsky", refAllsky);
  form.append("profile_name", $("newProfileName").value.trim());
  form.append("telescope_name", $("telescopeName").value.trim());
  form.append("camera_name", $("cameraName").value.trim());
  form.append("filter_name", $("filterName").value.trim());
  form.append("capture_gain_setting", $("captureGainSetting").value.trim());
  form.append("binning", $("profileBinning").value.trim());
  form.append("gain_e_per_adu", $("profileGain").value);
  form.append("read_noise_e", $("profileReadNoise").value);
  form.append("dark_current_e_per_pix_sec", $("profileDarkCurrent").value);
  form.append("noise_parameters_confirmed", $("profileNoiseConfirmed").checked ? "true" : "false");
  addMaybe(form, "bias_offset_adu", valueOrNull("profileBiasOffset"));
  form.append("extinction_k_mag_per_airmass", $("profileExtinction").value);
  addMaybe(form, "sensor_clip_adu", valueOrNull("profileSensorClip"));
  addMaybe(form, "pixel_scale_arcsec", valueOrNull("profilePixelScale"));
  addMaybe(form, "scope_exposure_sec", valueOrNull("profileScopeExposure"));
  addMaybe(form, "reference_allsky_exposure_sec", valueOrNull("profileAllskyExposure"));
  const t = state.referenceTarget || {};
  form.append("reference_target_name", t.name || "기준 영상");
  form.append("reference_target_type", t.object_type || "unknown");
  form.append("reference_target_mode", t.target_mode || "extended");
  addMaybe(form, "reference_target_mag", valueOrNull("profileReferenceMag") ?? t.vmag);
  addMaybe(form, "reference_target_size_deg", valueOrNull("profileReferenceSize") ?? t.size_deg);
  addMaybe(form, "reference_target_alt_deg", t.alt_deg);
  addMaybe(form, "reference_target_az_deg", t.az_deg);
  addMaybe(form, "reference_target_ra_deg", t.ra_deg);
  addMaybe(form, "reference_target_dec_deg", t.dec_deg);
  addMaybe(form, "reference_target_time_utc", t.time?.utc);
  addMaybe(form, "reference_target_time_local", t.time?.local);
  addMaybe(form, "reference_target_latitude", t.location?.latitude);
  addMaybe(form, "reference_target_longitude", t.location?.longitude);
  appendFiles(form, "scope_bias", $("profileScopeBias"));
  appendFiles(form, "scope_dark", $("profileScopeDark"));
  appendFiles(form, "scope_flat", $("profileScopeFlat"));
  appendFiles(form, "allsky_bias", $("profileAllskyBias"));
  appendFiles(form, "allsky_dark", $("profileAllskyDark"));
  appendFiles(form, "allsky_flat", $("profileAllskyFlat"));

  state.creatingProfile = true;
  $("createProfile").disabled = true;
  $("profileCreateStatus").textContent = "프로필 분석 중";
  try {
    const response = await fetch("/api/equipment/profiles/create", { method: "POST", body: form });
    const payload = await readJsonResponse(response);
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    const warningText = Array.isArray(payload.warnings) && payload.warnings.length ? ` · 확인 ${payload.warnings.length}` : "";
    $("profileCreateStatus").textContent = `생성 완료 · ${payload.confidence}${warningText}`;
    const cachedProfile = await cacheProfilePreviewData(payload);
    saveProfileSnapshot(cachedProfile);
    await loadProfiles(payload.profile_id);
  } catch (error) {
    $("profileCreateStatus").textContent = `프로필 생성 실패: ${error.message}`;
  } finally {
    state.creatingProfile = false;
    $("createProfile").disabled = false;
  }
}

let deleteProfileArmedUntil = 0;

async function deleteSelectedProfile() {
  const id = $("equipmentProfile").value;
  if (!id) {
    $("profileCreateStatus").textContent = "삭제할 장비 프로필을 먼저 선택하세요.";
    return;
  }
  const profile = state.profiles.find((item) => item.profile_id === id);
  const now = Date.now();
  if (now > deleteProfileArmedUntil) {
    deleteProfileArmedUntil = now + 5000;
    $("deleteProfile").textContent = "다시 눌러 삭제 확인";
    $("profileCreateStatus").textContent = `${profile?.name || "선택 프로필"}을 삭제하려면 5초 이내에 삭제 버튼을 다시 누르세요.`;
    window.setTimeout(() => {
      if (Date.now() >= deleteProfileArmedUntil) {
        $("deleteProfile").textContent = "선택 프로필 삭제";
      }
    }, 5100);
    return;
  }
  deleteProfileArmedUntil = 0;
  $("deleteProfile").disabled = true;
  $("deleteProfile").textContent = "삭제 중";
  try {
    const response = await fetch(`/api/equipment/profiles/${encodeURIComponent(id)}`, { method: "DELETE" });
    const payload = await readJsonResponse(response);
    if (!response.ok && response.status !== 404) throw new Error(payload.detail || `HTTP ${response.status}`);
    removeProfileSnapshot(id);
    $("profileCreateStatus").textContent = `${profile?.name || "장비 프로필"} 삭제 완료`;
    await loadProfiles();
  } catch (error) {
    $("profileCreateStatus").textContent = `프로필 삭제 실패: ${error.message}`;
  } finally {
    $("deleteProfile").disabled = false;
    $("deleteProfile").textContent = "선택 프로필 삭제";
  }
}

async function loadProfiles(selectId = null) {
  try {
    const response = await fetch("/api/equipment/profiles", { cache: "no-store" });
    const payload = await readJsonResponse(response);
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    const serverProfiles = payload.profiles || [];
    void cacheServerProfileSnapshots(serverProfiles);
    const snapshots = loadProfileSnapshots();
    const serverIds = new Set(serverProfiles.map((item) => item.profile_id));
    const recoveredProfiles = Object.values(snapshots)
      .filter((item) => item?.profile_id && !serverIds.has(item.profile_id))
      .map((item) => ({
        ...item,
        browser_recovery: true,
        scope_preview_url: item.scope_preview_data_url || null,
        allsky_preview_url: item.allsky_preview_data_url || null,
      }));
    state.profiles = [...serverProfiles, ...recoveredProfiles];
    const select = $("equipmentProfile");
    select.innerHTML = "";
    if (!state.profiles.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "장비 프로필 없음";
      select.appendChild(option);
    } else {
      state.profiles.forEach((profile) => {
        const option = document.createElement("option");
        option.value = profile.profile_id;
        option.textContent = profile.name;
        select.appendChild(option);
      });
      const remembered = localStorage.getItem("noxis.profileId");
      const preferred = selectId || remembered;
      if (preferred && state.profiles.some((item) => item.profile_id === preferred)) select.value = preferred;
      else select.value = state.profiles[0].profile_id;
      localStorage.setItem("noxis.profileId", select.value);
    }
    renderSelectedProfile();
  } catch {
    const snapshots = Object.values(loadProfileSnapshots()).filter((item) => item?.profile_id);
    state.profiles = snapshots.map((item) => ({
      ...item,
      browser_recovery: true,
      scope_preview_url: item.scope_preview_data_url || null,
      allsky_preview_url: item.allsky_preview_data_url || null,
    }));
    const select = $("equipmentProfile");
    select.innerHTML = "";
    if (state.profiles.length) {
      state.profiles.forEach((profile) => {
        const option = document.createElement("option");
        option.value = profile.profile_id;
        option.textContent = profile.name;
        select.appendChild(option);
      });
      const remembered = localStorage.getItem("noxis.profileId");
      if (remembered && state.profiles.some((item) => item.profile_id === remembered)) select.value = remembered;
      else select.value = state.profiles[0].profile_id;
      renderSelectedProfile();
    } else {
      select.innerHTML = '<option value="">프로필 불러오기 실패</option>';
    }
  }
  updateReadyState();
}

function setSavedPreview(imageId, url) {
  const image = $(imageId);
  if (!image) return;
  if (!url) { image.removeAttribute("src"); image.classList.add("empty-preview"); return; }
  image.classList.remove("empty-preview");
  image.src = String(url).startsWith("data:")
    ? url
    : `${url}${url.includes("?") ? "&" : "?"}v=${Date.now()}`;
}

function renderSelectedProfile() {
  const id = $("equipmentProfile").value;
  const profile = state.profiles.find((item) => item.profile_id === id);
  const box = $("selectedProfileSummary");
  const savedName = $("savedProfileName");
  if (!profile) {
    box.innerHTML = "<strong>장비 프로필</strong><span>장비를 등록하세요.</span>";
    if (savedName) savedName.textContent = "선택 없음";
    setSavedPreview("savedScopePreview", null);
    setSavedPreview("savedAllskyPreview", null);
    return;
  }
  box.innerHTML = "";
  const strong = document.createElement("strong"); strong.textContent = profile.name;
  const span = document.createElement("span");
  const gainLabel = profile.capture_gain_setting ? ` · ${profile.capture_gain_setting}` : "";
  span.textContent = `${profile.telescope_name || "망원경"} · ${profile.camera_name || "카메라"}${gainLabel}`;
  box.append(strong, span);
  if (savedName) savedName.textContent = profile.name;
  setSavedPreview("savedScopePreview", profile.scope_preview_url);
  setSavedPreview("savedAllskyPreview", profile.allsky_preview_url);
}

function updateReadyState() {
  const hasAllsky = Boolean(state.allskyToken || $("allsky")?.files?.[0]);
  const hasProfile = Boolean($("equipmentProfile").value);
  const hasAllskyExposure = Boolean(
    Number(state.allskyMetadata?.exposure_sec) > 0
      || Number(valueOrNull("allskyExposure")) > 0
      || state.allskyInspectFailed
  );
  const targetKnown = Boolean(state.target && Number.isFinite(Number(state.target.alt_deg)));
  const targetGood = targetKnown && Number(state.target.alt_deg) >= Number($("minimumSkyAltitude").value || 15);
  const targetFresh = Boolean(state.stellariumFresh);
  const ready = Boolean(
    hasAllsky && hasAllskyExposure && hasProfile && targetGood && targetFresh
      && !state.analyzing && !state.allskyInspecting
  );
  const button = $("analyzeButton");
  button.disabled = !ready;

  if (state.analyzing) {
    button.textContent = "분석 중…";
    $("readyStatus").textContent = "";
  } else if (!hasAllsky) {
    button.textContent = "전천 영상을 선택하세요";
    $("readyStatus").textContent = "";
  } else if (state.allskyInspecting) {
    button.textContent = "영상 헤더 읽는 중…";
    $("readyStatus").textContent = "노출시간과 미리보기를 확인하고 있습니다.";
  } else if (!hasAllskyExposure) {
    button.textContent = "전천 노출시간을 입력하세요";
    $("readyStatus").textContent = "영상 헤더에서 노출시간을 읽지 못했습니다.";
  } else if (state.allskyInspectFailed) {
    button.textContent = "분석하며 영상 다시 읽기";
    $("readyStatus").textContent = "빠른 검사가 중단되어 분석 단계에서 원본 헤더를 다시 확인합니다.";
  } else if (!hasProfile) {
    button.textContent = "장비 프로필을 선택하세요";
    $("readyStatus").textContent = "";
  } else if (!targetKnown) {
    button.textContent = "Stellarium에서 천체를 선택하세요";
    $("readyStatus").textContent = "";
  } else if (!targetGood) {
    button.textContent = "대상 고도를 확인하세요";
    $("readyStatus").textContent = "";
  } else if (!targetFresh) {
    button.textContent = "Stellarium 재연결 중";
    $("readyStatus").textContent = "마지막 천체는 유지 중입니다.";
  } else {
    button.textContent = "분석";
    $("readyStatus").textContent = state.analysisError || "";
  }
  if (state.analysisError && !state.analyzing) $("readyStatus").textContent = state.analysisError;
}


function setSessionLocked(locked) {
  const selectors = [
    "#allskyPanel input", "#targetPanel button", "#planningPanel input",
    "#planningPanel select", "#planningPanel button", "#openProfileManager",
  ];
  document.querySelectorAll(selectors.join(",")).forEach((element) => {
    element.disabled = Boolean(locked);
  });
}

function startProgress() {
  state.analysisError = null;
  state.analyzing = true;
  setSessionLocked(true);
  $("progressBox").classList.remove("hidden");
  $("analyzeButton").disabled = true;
  updateReadyState();
}

function stopProgress() {
  state.analyzing = false;
  setSessionLocked(false);
  $("progressBox").classList.add("hidden");
  updateReadyState();
}

function buildAnalysisForm({ useToken = true } = {}) {
  const form = new FormData();
  const file = $("allsky").files[0];
  if (useToken && state.allskyToken) form.append("allsky_token", state.allskyToken);
  else if (file) form.append("allsky", file);
  const selectedProfileId = $("equipmentProfile").value;
  form.append("profile_id", selectedProfileId);
  const profileSnapshot = getProfileSnapshot(selectedProfileId);
  if (profileSnapshot) form.append("profile_snapshot_json", JSON.stringify(profileSnapshot));
  const target = state.target;
  form.append("target_name", target.name || "선택 천체");
  form.append("target_object_type", target.object_type || "unknown");
  form.append("target_mode", target.target_mode || "extended");
  addMaybe(form, "target_vmag", target.vmag); addMaybe(form, "target_vmage", target.vmage);
  addMaybe(form, "target_size_deg", target.size_deg); addMaybe(form, "target_alt_deg", target.alt_deg); addMaybe(form, "target_az_deg", target.az_deg);
  addMaybe(form, "target_ra_deg", target.ra_deg); addMaybe(form, "target_dec_deg", target.dec_deg);
  addMaybe(form, "target_time_utc", target.time?.utc); addMaybe(form, "target_time_local", target.time?.local);
  addMaybe(form, "target_latitude", target.location?.latitude); addMaybe(form, "target_longitude", target.location?.longitude);
  addMaybe(form, "allsky_exposure_sec", valueOrNull("allskyExposure"));
  addMaybe(form, "allsky_bias_offset_adu", valueOrNull("allskyBiasOffset"));
  form.append("target_snr", $("targetSnr").value); form.append("min_sub_exposure_sec", $("minExposure").value);
  form.append("max_sub_exposure_sec", $("maxExposure").value); form.append("tracking_limit_sec", $("trackingLimit").value);
  form.append("background_limit_fraction", $("backgroundLimit").value); form.append("saturation_safety_fraction", $("saturationSafety").value);
  form.append("stack_efficiency", $("stackEfficiency").value); form.append("max_frames", $("maxFrames").value);
  form.append("frame_overhead_sec", $("frameOverhead").value); form.append("effective_pixels", $("effectivePixels").value);
  form.append("minimum_sky_altitude_deg", $("minimumSkyAltitude").value); form.append("az_bins", $("azBins").value); form.append("alt_bins", $("altBins").value);
  addMaybe(form, "manual_target_mag", valueOrNull("manualTargetMag"));
  addMaybe(form, "manual_surface_brightness_mag_arcsec2", valueOrNull("manualSurfaceBrightness"));
  appendFiles(form, "allsky_bias", $("allskyBias")); appendFiles(form, "allsky_dark", $("allskyDark")); appendFiles(form, "allsky_flat", $("allskyFlat"));
  return form;
}

async function submitAnalysis(useToken = true) {
  const response = await fetch("/api/session/analyze", { method: "POST", body: buildAnalysisForm({ useToken }) });
  const payload = await readJsonResponse(response);
  return { response, payload };
}

async function analyzeSession() {
  if ($("analyzeButton").disabled) return;
  if (!state.target) {
    const imported = await importStellariumTarget(false, { quiet: true });
    if (!imported) { updateReadyState(); return; }
  }
  const minAlt = Number($("minimumSkyAltitude").value || 15);
  if (Number(state.target.alt_deg) < minAlt) { updateTargetAltitudeStatus(); return; }
  startProgress();
  try {
    let { response, payload } = await submitAnalysis(Boolean(state.allskyToken));
    if (!response.ok && state.allskyToken && $("allsky").files[0] && response.status === 410) {
      state.allskyToken = null;
      ({ response, payload } = await submitAnalysis(false));
    }
    if (!response.ok) {
      if (response.status === 404) throw new Error("서버가 재시작되어 장비 프로필을 찾지 못했습니다. 장비 프로필을 한 번 다시 저장하면 이후에는 자동 복구됩니다.");
      if (response.status === 410) throw new Error("서버가 재시작되어 전천 영상 임시파일이 만료되었습니다. 같은 파일을 다시 선택하세요.");
      throw new Error(payload.detail || "분석 실패");
    }
    state.analysisError = null;
    renderResult(payload);
  } catch (error) {
    state.analysisError = `분석 실패 · ${error.message}`;
    $("readyStatus").textContent = state.analysisError;
    $("analyzeButton").textContent = "다시 분석";
  } finally {
    stopProgress();
  }
}


function validityLabel(value) {
  return { quantitative_candidate: "정량 검증 후보", planning_only: "관측 계획용", diagnostic_only: "진단용", invalid: "입력 확인 필요" }[value] || value;
}

function confidenceLabel(value) { return { high: "높음", medium: "보통", low: "낮음", none: "없음" }[value] || value || "낮음"; }

function exposureSelectionLabel(value) {
  return {
    efficiency_target: "효율 90% 도달",
    hard_upper_before_efficiency_target: "안전 상한 우선",
    reference_star_advisory_before_efficiency_target: "기준별 포화 권고 우선",
    no_feasible_interval: "가능 구간 없음",
  }[value] || value || "선택 근거 없음";
}

function renderList(container, items) {
  container.innerHTML = "";
  (items || []).forEach((item) => {
    const div = document.createElement("div");
    div.textContent = String(item);
    container.appendChild(div);
  });
}

function galleryCard(key, url) {
  if (!url || !/\.(png|jpg|jpeg|webp)$/i.test(url)) return null;
  const [title, description] = ARTIFACT_INFO[key] || [key, "결과 이미지"];
  const article = document.createElement("article");
  const img = document.createElement("img");
  img.src = url;
  img.alt = title;
  img.loading = "lazy";
  img.addEventListener("click", () => openImageModal(url, title));
  const copy = document.createElement("div");
  copy.className = "card-copy";
  const h = document.createElement("h4"); h.textContent = title;
  const a = document.createElement("a"); a.href = url; a.download = ""; a.className = "secondary compact"; a.textContent = "저장";
  copy.append(h);
  if (description) { const p = document.createElement("p"); p.textContent = description; copy.append(p); }
  copy.append(a); article.append(img, copy); return article;
}

function populateGallery(containerId, keys, artifacts) {
  const container = $(containerId); container.innerHTML = "";
  keys.forEach((key) => { const card = galleryCard(key, artifacts[key]); if (card) container.appendChild(card); });
}

function flattenObject(object, prefix = "", output = []) {
  Object.entries(object || {}).forEach(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) flattenObject(value, path, output);
    else output.push([path, Array.isArray(value) ? value.join(", ") : value]);
  });
  return output;
}

function diagnosticTable(title, object) {
  const wrapper = document.createElement("section"); wrapper.className = "table-card";
  const h = document.createElement("h3"); h.textContent = title;
  const table = document.createElement("table"); const tbody = document.createElement("tbody");
  flattenObject(object).forEach(([key, value]) => {
    const row = document.createElement("tr"); const th = document.createElement("th"); const td = document.createElement("td");
    th.textContent = key; td.textContent = value === null || value === undefined ? "—" : String(value); row.append(th, td); tbody.appendChild(row);
  });
  table.appendChild(tbody); wrapper.append(h, table); return wrapper;
}

function renderResult(result) {
  $("resultSection").classList.remove("hidden");
  const plan = result.plan || {};
  $("validityBadge").textContent = validityLabel(result.validity);
  $("resultTargetName").textContent = result.target?.name || "분석 결과";
  $("resultDetail").textContent = `${result.equipment_profile?.name || "장비 프로필"} · ${result.target?.object_type || "천체"} · 단일노출 ${exposureSelectionLabel(plan.selection_basis)} · 신호 모델 ${result.target_signal_model?.source || "없음"}`;
  $("confidenceBox").textContent = `신뢰도 ${confidenceLabel(result.confidence)} · ${validityLabel(result.validity)}`;
  $("mSub").textContent = plan.recommended_sub_exposure_sec == null ? "확정 불가" : formatSubExposureSeconds(plan.recommended_sub_exposure_sec);
  const subRange = plan.recommended_sub_exposure_range_sec;
  $("mSub").title = Array.isArray(subRange) && subRange.length === 2
    ? `배경 불확실성 범위 ${formatSubExposureSeconds(subRange[0])} – ${formatSubExposureSeconds(subRange[1])}`
    : "";
  $("mSnr").textContent = formatNumber(plan.predicted_snr_per_sub, 2);
  $("mFrames").textContent = plan.frames == null
    ? (plan.max_frames_exceeded ? `필요 ${formatNumber(plan.required_frames_unbounded, 0)}장` : "—")
    : `${formatNumber(plan.frames, 0)}장`;
  const frameRange = plan.required_frames_range;
  $("mFrames").title = Array.isArray(frameRange) && frameRange.length === 2
    ? `신호·배경 불확실성 범위 ${formatNumber(frameRange[0], 0)} – ${formatNumber(frameRange[1], 0)}장`
    : "";
  $("mTotal").textContent = formatSeconds(plan.total_integration_sec);
  $("mAltitude").textContent = `${formatNumber(result.target?.alt_deg, 2)}°`;
  $("mAirmass").textContent = result.target?.airmass == null ? "—" : formatNumber(result.target.airmass, 5);
  $("mBackground").textContent = `${formatNumber(result.background_model?.telescope_background_e_per_sec_per_pixel, 3)} e⁻/s/pix`;
  renderList($("validityReasons"), result.validity_reasons || []);
  renderList($("warningList"), result.warnings || []);
  const artifacts = result.artifacts || {};
  populateGallery("overviewGallery", ["sky_polar_map", "exposure_snr_curve", "allsky_coordinate_overlay", "sky_reliability"], artifacts);
  populateGallery("skyGallery", ["sky_polar_map", "allsky_coordinate_overlay", "sky_relative_map", "sky_reliability", "sky_altitude_profiles", "sky_map", "sky_distribution", "allsky_preview"], artifacts);
  const tables = $("diagnosticTables"); tables.innerHTML = "";
  tables.append(
    diagnosticTable("노출 계획", result.plan),
    diagnosticTable("대상 신호 모델", result.target_signal_model),
    diagnosticTable("하늘 배경 환산", result.background_model),
    diagnosticTable("장비 프로필", result.equipment_profile),
    diagnosticTable("전천지도 요약", {
      sky_median_adu: result.sky?.sky_median_adu,
      target_background_adu: result.sky?.target_background_adu,
      target_relative_factor: result.sky?.target_relative_factor,
      target_uncertainty_adu: result.sky?.target_uncertainty_adu,
      good_fraction: result.sky?.good_fraction,
      blocked_fraction: result.sky?.blocked_fraction,
      usable_fraction: result.sky?.usable_fraction,
    }),
    diagnosticTable("기타 진단", result.diagnostics),
  );
  $("rawJson").textContent = JSON.stringify(result, null, 2);
  const downloads = $("downloadLinks"); downloads.innerHTML = "";
  [["sky_table", "전천지도 TSV"], ["result_json", "결과 JSON"]].forEach(([key, label]) => {
    if (!artifacts[key]) return; const a = document.createElement("a"); a.href = artifacts[key]; a.download = ""; a.className = "secondary small"; a.textContent = label; downloads.appendChild(a);
  });
  document.querySelectorAll(".tab-content").forEach((panel) => panel.classList.remove("active"));
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.remove("active"));
  $("overviewTab").classList.add("active"); document.querySelector('[data-tab="overviewTab"]').classList.add("active");
  $("resultSection").scrollIntoView({ behavior: "smooth", block: "start" });
}

function openImageModal(url, caption) { $("modalImage").src = url; $("modalCaption").textContent = caption; $("modalDownload").href = url; $("imageModal").classList.remove("hidden"); }

function closeProfileManager() {
  $("profileManager").classList.add("hidden");
  document.body.classList.remove("profile-open");
}

async function syncStellariumTarget() {
  if (state.analyzing || state.stellariumSyncInFlight || document.visibilityState !== "visible") return;
  state.stellariumSyncInFlight = true;
  try {
    await importStellariumTarget(false, { quiet: true });
  } finally {
    state.stellariumSyncInFlight = false;
  }
}

async function initializeStellariumAutoSync() {
  const connected = await stellariumPing({ quiet: true });
  if (connected) await syncStellariumTarget();
  if (state.stellariumAutoTimer) window.clearInterval(state.stellariumAutoTimer);
  state.stellariumAutoTimer = window.setInterval(syncStellariumTarget, 6000);
}

function wireEvents() {
  document.querySelectorAll("[data-scroll]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item[data-scroll]").forEach((item) => item.classList.toggle("active", item === button));
    const target = $(button.dataset.scroll);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  $("openProfileManager").addEventListener("click", () => {
    $("profileManager").classList.remove("hidden");
    document.body.classList.add("profile-open");
  });
  $("closeProfileManager").addEventListener("click", closeProfileManager);
  $("profileManager").addEventListener("click", (event) => { if (event.target === $("profileManager")) closeProfileManager(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("profileManager").classList.contains("hidden")) closeProfileManager(); });
  $("profileScopeFile").addEventListener("change", () => inspectReferenceScope($("profileScopeFile").files[0]));
  $("profileAllskyFile").addEventListener("change", () => inspectReferenceAllsky($("profileAllskyFile").files[0]));
  $("allsky").addEventListener("change", () => inspectAllsky($("allsky").files[0]));
  $("allskyExposure").addEventListener("input", updateReadyState);
  $("stellariumPing").addEventListener("click", stellariumPing);
  $("stellariumPingDaily").addEventListener("click", stellariumPing);
  $("importReferenceTarget").addEventListener("click", () => importStellariumTarget(true));
  $("setStellariumReferenceTime").addEventListener("click", setStellariumReferenceTime);
  $("setStellariumNow").addEventListener("click", setStellariumNow);
  $("importTarget").addEventListener("click", () => importStellariumTarget(false));
  $("createProfile").addEventListener("click", createEquipmentProfile);
  $("deleteProfile").addEventListener("click", deleteSelectedProfile);
  $("equipmentProfile").addEventListener("change", () => {
    if ($("equipmentProfile").value) localStorage.setItem("noxis.profileId", $("equipmentProfile").value);
    renderSelectedProfile(); updateReadyState();
  });
  $("minimumSkyAltitude").addEventListener("input", updateTargetAltitudeStatus);
  $("targetSnr").addEventListener("input", () => {
    document.querySelectorAll('.preset-row[data-target="targetSnr"] button').forEach((button) => {
      button.classList.toggle("active", Number(button.dataset.value) === Number($("targetSnr").value));
    });
  });
  $("analyzeButton").addEventListener("click", analyzeSession);
  document.querySelectorAll(".preset-row button").forEach((button) => button.addEventListener("click", () => {
    const row = button.closest(".preset-row"); const input = $(row.dataset.target); input.value = button.dataset.value;
    row.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
  }));
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active"); $(tab.dataset.tab).classList.add("active");
  }));
  $("stellariumUrl").addEventListener("change", () => localStorage.setItem("noxis.stellariumUrl", $("stellariumUrl").value.trim()));
  $("stellariumPassword").addEventListener("input", () => sessionStorage.setItem("noxis.stellariumPassword", $("stellariumPassword").value));
  $("closeModal").addEventListener("click", () => $("imageModal").classList.add("hidden"));
  $("imageModal").addEventListener("click", (event) => { if (event.target === $("imageModal")) $("imageModal").classList.add("hidden"); });
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") syncStellariumTarget(); });
}

window.addEventListener("DOMContentLoaded", async () => {
  const savedUrl = localStorage.getItem("noxis.stellariumUrl");
  if (savedUrl) $("stellariumUrl").value = savedUrl;
  const savedPassword = sessionStorage.getItem("noxis.stellariumPassword");
  if (savedPassword) $("stellariumPassword").value = savedPassword;
  wireEvents();
  await loadProfiles();
  updateReadyState();
  window.setTimeout(initializeStellariumAutoSync, 350);
});
