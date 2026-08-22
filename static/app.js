"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  allskyToken: null,
  allskyMetadata: null,
  target: null,
  referenceTarget: null,
  profiles: [],
  analyzing: false,
  creatingProfile: false,
  allskyInspectSequence: 0,
  allskyInspectController: null,
  referenceScopeMetadata: null,
  referenceScopeCaptureTimeUtc: null,
  referenceScopeInspectSequence: 0,
  referenceScopeInspectController: null,
  referenceAllskyMetadata: null,
  referenceAllskyInspectSequence: 0,
  referenceAllskyInspectController: null,
  objectUrls: new Map(),
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

function escapeText(value) { return String(value ?? ""); }

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
  if (/401|403/.test(message)) return "Remote Control 암호 확인";
  if (/permission|denied|address space|local network/i.test(message)) return "브라우저의 로컬 네트워크 권한 허용 필요";
  if (/Failed to fetch|NetworkError|Load failed|fetch/i.test(message)) return "Stellarium Remote Control/CORS 확인";
  return message || "연결 실패";
}

async function normalizeStellariumPayload(info, status) {
  const response = await fetch("/api/stellarium/normalize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ info, status }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "천체 정보 해석 실패");
  return payload;
}

function dateToJulianDay(iso) {
  const milliseconds = Date.parse(iso);
  if (!Number.isFinite(milliseconds)) throw new Error("촬영시각 확인");
  return milliseconds / 86400000 + 2440587.5;
}

function renderMetadata(metadata) {
  if (!metadata) return "메타데이터 없음";
  const items = [
    `${metadata.width} × ${metadata.height}`,
    metadata.source_type?.toUpperCase(),
    metadata.exposure_sec ? `노출 ${metadata.exposure_sec}s` : "노출시간 미확인",
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
  try { return JSON.parse(text); } catch { return { detail: text.slice(0, 300) }; }
}

async function inspectAllsky(file) {
  state.allskyInspectSequence += 1;
  const sequence = state.allskyInspectSequence;
  if (state.allskyInspectController) state.allskyInspectController.abort();
  state.allskyInspectController = new AbortController();
  state.allskyToken = null;
  state.allskyMetadata = null;
  showLocalPreview(file, "allskyPreview", "main-allsky");
  if (!file) {
    $("allskyName").textContent = "전천 영상 선택";
    $("allskyPreviewStatus").textContent = "";
    $("allskyMetadata").textContent = "";
    updateReadyState();
    return;
  }
  $("allskyName").textContent = file.name;
  $("allskyPreviewStatus").textContent = "읽는 중";
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
    showServerPreview(payload.preview_url, "allskyPreview");
    $("allskyPreviewStatus").textContent = payload.preview_warning ? "미리보기 제한" : "";
    $("allskyMetadata").textContent = renderMetadata(payload.metadata);
    if (payload.metadata?.exposure_sec && $("allskyExposure").value === "") {
      $("allskyExposure").placeholder = `헤더: ${payload.metadata.exposure_sec}s`;
    }
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.allskyInspectSequence) return;
    // Browser preview may still be usable. Analysis retries with the original file.
    $("allskyPreviewStatus").textContent = "서버 확인 필요";
    $("allskyMetadata").textContent = error.message || "전천 영상 읽기 실패";
  } finally {
    if (sequence === state.allskyInspectSequence) {
      state.allskyInspectController = null;
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
    $("profileScopeName").textContent = "파일 선택";
    $("profileScopeMetadata").textContent = "";
    $("profileScopePreviewStatus").textContent = "";
    $("profileTimeStatus").textContent = "";
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
    $("profileScopePreviewStatus").textContent = payload.preview_warning ? "미리보기 제한" : "";
    $("profileScopeMetadata").textContent = formatCaptureMetadata(payload.metadata, payload.capture_time_utc);
    $("setStellariumReferenceTime").disabled = !payload.capture_time_utc;
    if (payload.metadata?.exposure_sec && $("profileScopeExposure").value === "") {
      $("profileScopeExposure").placeholder = `헤더: ${payload.metadata.exposure_sec}s`;
    }
    if (!$("cameraName").value.trim() && payload.metadata?.camera) $("cameraName").value = payload.metadata.camera;
    if (!$("captureGainSetting").value.trim() && payload.metadata?.gain_setting) $("captureGainSetting").value = `Gain/ISO ${payload.metadata.gain_setting}`;
    $("profileTimeStatus").textContent = payload.capture_time_utc ? "촬영시각 확인" : "";
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.referenceScopeInspectSequence) return;
    $("profileScopePreviewStatus").textContent = "서버 확인 필요";
    $("profileScopeMetadata").textContent = error.message;
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
    $("profileAllskyName").textContent = "선택 안 함";
    $("profileAllskyMetadata").textContent = "";
    $("profileAllskyPreviewStatus").textContent = "";
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
    $("profileAllskyPreviewStatus").textContent = payload.preview_warning ? "미리보기 제한" : "";
    $("profileAllskyMetadata").textContent = renderMetadata(payload.metadata);
    if (payload.metadata?.exposure_sec && $("profileAllskyExposure").value === "") {
      $("profileAllskyExposure").placeholder = `헤더: ${payload.metadata.exposure_sec}s`;
    }
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.referenceAllskyInspectSequence) return;
    $("profileAllskyPreviewStatus").textContent = "서버 확인 필요";
    $("profileAllskyMetadata").textContent = error.message;
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
    card.textContent = "선택 안 됨";
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
  try {
    await stellariumRequest("/api/main/status");
    status.textContent = "연결됨";
    status.className = "status-text ok";
    return true;
  } catch (error) {
    status.textContent = quiet ? "Stellarium 연결 필요" : `연결 실패 · ${stellariumFailureMessage(error)}`;
    status.className = "status-text error";
    return false;
  }
}

async function importStellariumTarget(reference = false, { quiet = false } = {}) {
  const statusText = $("stellariumStatus");
  statusText.className = "status-text";
  if (!quiet) statusText.textContent = "가져오는 중";
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
    if (reference) {
      state.referenceTarget = target;
      renderTargetCard(target, true);
      statusText.textContent = `기준 · ${target.name}`;
    } else {
      state.target = target;
      renderTargetCard(target, false);
      statusText.textContent = target.name;
    }
    statusText.className = "status-text ok";
    updateReadyState();
    return target;
  } catch (error) {
    statusText.textContent = `Stellarium · ${stellariumFailureMessage(error)}`;
    statusText.className = "status-text error";
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
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "프로필 생성 실패");
    const warningText = Array.isArray(payload.warnings) && payload.warnings.length ? ` · 확인 ${payload.warnings.length}` : "";
    $("profileCreateStatus").textContent = `생성 완료 · ${payload.confidence}${warningText}`;
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
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
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
    const payload = await response.json();
    state.profiles = payload.profiles || [];
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
    const select = $("equipmentProfile");
    select.innerHTML = '<option value="">프로필 불러오기 실패</option>';
  }
  updateReadyState();
}

function setSavedPreview(imageId, url) {
  const image = $(imageId);
  if (!image) return;
  if (!url) { image.removeAttribute("src"); image.classList.add("empty-preview"); return; }
  image.classList.remove("empty-preview");
  image.src = `${url}${url.includes("?") ? "&" : "?"}v=${Date.now()}`;
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
  const targetKnown = Boolean(state.target && Number.isFinite(Number(state.target.alt_deg)));
  const targetGood = !targetKnown || Number(state.target.alt_deg) >= Number($("minimumSkyAltitude").value || 15);
  const ready = Boolean(hasAllsky && hasProfile && targetGood && !state.analyzing);
  $("analyzeButton").disabled = !ready;
  if (state.analyzing) $("readyStatus").textContent = "분석 중";
  else if (!hasAllsky) $("readyStatus").textContent = "전천 영상 필요";
  else if (!hasProfile) $("readyStatus").textContent = "장비 프로필 필요";
  else if (targetKnown && !targetGood) $("readyStatus").textContent = "대상 고도 확인";
  else if (!state.target) $("readyStatus").textContent = "대상 자동 확인";
  else $("readyStatus").textContent = "준비 완료";
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
  form.append("profile_id", $("equipmentProfile").value);
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
    $("readyStatus").textContent = "Stellarium 대상 확인 중";
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
    if (!response.ok) throw new Error(payload.detail || "분석 실패");
    renderResult(payload);
  } catch (error) {
    $("readyStatus").textContent = `분석 실패 · ${error.message}`;
  } finally {
    stopProgress();
  }
}


function validityLabel(value) {
  return { quantitative_candidate: "정량 검증 후보", planning_only: "관측 계획용", diagnostic_only: "진단용", invalid: "입력 확인 필요" }[value] || value;
}

function confidenceLabel(value) { return { high: "높음", medium: "보통", low: "낮음", none: "없음" }[value] || value || "낮음"; }

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
  $("resultDetail").textContent = `${result.equipment_profile?.name || "장비 프로필"} · ${result.target?.object_type || "천체"} · 신호 모델 ${result.target_signal_model?.source || "없음"}`;
  $("confidenceBox").textContent = `신뢰도 ${confidenceLabel(result.confidence)} · ${validityLabel(result.validity)}`;
  $("mSub").textContent = plan.recommended_sub_exposure_sec == null ? "확정 불가" : formatSeconds(plan.recommended_sub_exposure_sec);
  $("mSnr").textContent = formatNumber(plan.predicted_snr_per_sub, 2);
  $("mFrames").textContent = plan.frames == null ? "—" : `${formatNumber(plan.frames, 0)}장`;
  $("mTotal").textContent = formatSeconds(plan.total_integration_sec);
  $("mAltitude").textContent = `${formatNumber(result.target?.alt_deg, 2)}°`;
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
}

window.addEventListener("DOMContentLoaded", async () => {
  const savedUrl = localStorage.getItem("noxis.stellariumUrl");
  if (savedUrl) $("stellariumUrl").value = savedUrl;
  const savedPassword = sessionStorage.getItem("noxis.stellariumPassword");
  if (savedPassword) $("stellariumPassword").value = savedPassword;
  wireEvents();
  await loadProfiles();
  updateReadyState();
  window.setTimeout(() => stellariumPing({ quiet: true }), 350);
});
