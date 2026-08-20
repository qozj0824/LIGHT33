"use strict";

const $ = (id) => document.getElementById(id);
const LOCAL_PROFILE_KEY = "lightt.equipmentProfiles.v35";
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
  serverConfig: { hosted: false, profile_storage: "server" },
};

const ARTIFACT_INFO = {
  sky_polar_map: ["원형 방향별 하늘 배경 지도", "중심은 천정, 외곽은 저고도 방향입니다."],
  allsky_coordinate_overlay: ["전천 좌표 오버레이", "분석 영역과 선택 천체 방향을 원본 영상에서 확인합니다."],
  sky_relative_map: ["상대 하늘 배경 지도", "전천 중앙값에 대한 방향별 상대 밝기입니다."],
  sky_reliability: ["전천 셀 신뢰도", "good/caution/low/blocked/missing 판정을 표시합니다."],
  sky_altitude_profiles: ["고도별 방위 프로파일", "고도에 따른 방위별 배경 변화입니다."],
  sky_map: ["방위각–고도 배경 ADU 지도", "분석용 직사각형 좌표 지도입니다."],
  sky_distribution: ["배경값 분포", "유효 하늘 셀의 배경 분포입니다."],
  allsky_preview: ["전천 영상", "분석에 사용한 전천 영상 미리보기입니다."],
  exposure_snr_curve: ["노출시간–SNR 모델", "저장된 장비 프로필과 선택 천체 광도 모델로 계산합니다."],
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

async function checkServer() {
  try {
    const response = await fetch("/health");
    const payload = await response.json();
    state.serverConfig = payload || state.serverConfig;
    $("serverStatus").textContent = payload.ok
      ? `${payload.hosted ? "웹 서버" : "서버"} 정상 · v${payload.version}`
      : "서버 오류";
  } catch {
    $("serverStatus").textContent = "서버 연결 실패";
  }
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

async function inspectAllsky(file) {
  state.allskyInspectSequence += 1;
  const sequence = state.allskyInspectSequence;
  if (state.allskyInspectController) state.allskyInspectController.abort();
  state.allskyInspectController = new AbortController();
  state.allskyToken = null;
  state.allskyMetadata = null;
  if (!file) {
    $("allskyName").textContent = "파일 선택";
    $("allskyPreviewStatus").textContent = "대기 중";
    $("allskyMetadata").textContent = "파일을 선택하면 형식·크기·노출시간을 확인합니다.";
    $("allskyPreview").removeAttribute("src");
    updateReadyState();
    return;
  }
  $("allskyName").textContent = file.name;
  $("allskyPreviewStatus").textContent = "확인 중";
  const form = new FormData();
  form.append("file", file);
  form.append("role", "allsky");
  try {
    const response = await fetch("/api/inspect", {
      method: "POST", body: form, signal: state.allskyInspectController.signal,
    });
    const payload = await response.json();
    if (sequence !== state.allskyInspectSequence) return;
    if (!response.ok) throw new Error(payload.detail || "미리보기 실패");
    state.allskyToken = payload.upload_token;
    state.allskyMetadata = payload.metadata;
    $("allskyPreview").src = payload.preview_url;
    $("allskyPreviewStatus").textContent = "확인 완료";
    $("allskyMetadata").textContent = renderMetadata(payload.metadata);
    if (payload.metadata?.exposure_sec && $("allskyExposure").value === "") {
      $("allskyExposure").placeholder = `헤더: ${payload.metadata.exposure_sec}s`;
    }
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.allskyInspectSequence) return;
    $("allskyPreviewStatus").textContent = "확인 실패";
    $("allskyMetadata").textContent = error.message;
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
  if (!file) {
    $("profileScopeName").textContent = "파일 선택";
    $("profileScopeMetadata").textContent = "파일을 선택하면 촬영시각과 메타데이터를 자동 확인합니다.";
    $("profileTimeStatus").textContent = "촬영시각이 달라도 장비 프로필 생성 자체는 중단하지 않습니다.";
    return;
  }
  $("profileScopeName").textContent = file.name;
  $("profileScopeMetadata").textContent = "메타데이터 확인 중";
  const form = new FormData();
  form.append("file", file);
  form.append("role", "scope");
  try {
    const response = await fetch("/api/inspect", {
      method: "POST", body: form, signal: state.referenceScopeInspectController.signal,
    });
    const payload = await response.json();
    if (sequence !== state.referenceScopeInspectSequence) return;
    if (!response.ok) throw new Error(payload.detail || "기준 영상 확인 실패");
    state.referenceScopeMetadata = payload.metadata;
    state.referenceScopeCaptureTimeUtc = payload.capture_time_utc || null;
    $("profileScopeMetadata").textContent = formatCaptureMetadata(payload.metadata, payload.capture_time_utc);
    $("setStellariumReferenceTime").disabled = !payload.capture_time_utc;
    if (payload.metadata?.exposure_sec && $("profileScopeExposure").value === "") {
      $("profileScopeExposure").placeholder = `헤더: ${payload.metadata.exposure_sec}s`;
    }
    if (!$("cameraName").value.trim() && payload.metadata?.camera) $("cameraName").value = payload.metadata.camera;
    if (!$("captureGainSetting").value.trim() && payload.metadata?.gain_setting) {
      $("captureGainSetting").value = `Gain/ISO ${payload.metadata.gain_setting}`;
    }
    $("profileTimeStatus").textContent = payload.capture_time_utc
      ? "촬영시각을 확인했습니다. Stellarium 시각이 달라도 RA/Dec가 있으면 서버가 촬영 당시 Alt/Az를 재계산합니다."
      : "촬영시각의 시간대를 확정하지 못했습니다. 프로필은 만들 수 있지만 Csys·기준 대기질량 등 시간 의존 보정은 제한됩니다.";
  } catch (error) {
    if (error?.name === "AbortError" || sequence !== state.referenceScopeInspectSequence) return;
    $("profileScopeMetadata").textContent = error.message;
    $("profileTimeStatus").textContent = "메타데이터를 읽지 못해도 기준 영상 자체는 프로필 생성 단계에서 다시 검사합니다.";
  } finally {
    if (sequence === state.referenceScopeInspectSequence) state.referenceScopeInspectController = null;
  }
}

function normalizeStellariumBase(rawValue) {
  const raw = (rawValue || "http://127.0.0.1:8090").trim();
  const url = new URL(raw.includes("://") ? raw : `http://${raw}`);
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Stellarium 주소는 http/https만 사용할 수 있습니다.');
  if (url.username || url.password || (url.pathname && url.pathname !== '/') || url.search || url.hash) {
    throw new Error('Stellarium 기본 주소에는 경로·계정·query를 넣지 마세요.');
  }
  return url.origin;
}

function stellariumCorsHint() {
  return 'Stellarium Remote Control에서 CORS를 켜고 허용 Origin을 이 사이트 주소로 설정하세요. 테스트할 때만 *를 사용할 수 있습니다.';
}

async function fetchLocalStellarium(path, options = {}) {
  const base = normalizeStellariumBase($("stellariumUrl").value);
  const host = new URL(base).hostname;
  const targetAddressSpace = (host === '127.0.0.1' || host === 'localhost' || host === '::1') ? 'loopback' : 'local';
  const requestOptions = {
    mode: 'cors',
    cache: 'no-store',
    ...options,
    targetAddressSpace,
  };
  return fetch(`${base}${path}`, requestOptions);
}

async function setStellariumTime(observationTimeUtc, pause = true, label = "기준 촬영시각") {
  $("profileTimeStatus").textContent = `Stellarium을 ${label}으로 설정하는 중`;
  try {
    const date = new Date(observationTimeUtc);
    if (Number.isNaN(date.getTime())) throw new Error('촬영시각을 UTC 시각으로 해석하지 못했습니다.');
    const julianDay = date.getTime() / 86400000 + 2440587.5;
    const timerate = pause ? 0 : (1 / 86400);
    const body = new URLSearchParams({ time: julianDay.toFixed(10), timerate: String(timerate) });
    const response = await fetchLocalStellarium('/api/main/time', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
      body,
    });
    const text = await response.text();
    if (!response.ok) throw new Error(text || `HTTP ${response.status}`);
    $("profileTimeStatus").textContent = pause
      ? `Stellarium을 기준 촬영시각(${date.toISOString()})으로 맞췄습니다. 필요하면 기준 천체를 다시 가져오세요.`
      : 'Stellarium을 현재 시각으로 복귀시키고 시간 흐름을 재개했습니다.';
    $("stellariumStatus").textContent = 'Stellarium 시각 설정 완료';
  } catch (error) {
    $("profileTimeStatus").textContent = `Stellarium 시각 설정 실패: ${error.message} · ${stellariumCorsHint()}`;
  }
}

async function setStellariumReferenceTime() {
  if (!state.referenceScopeCaptureTimeUtc) {
    $("profileTimeStatus").textContent = "기준 영상에서 시간대가 확정된 촬영시각을 읽지 못했습니다.";
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
    card.textContent = reference ? "기준 천체를 아직 가져오지 않았습니다." : "관측 대상이 선택되지 않았습니다.";
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
    box.querySelector("span").textContent = "선택 천체의 고도를 확인할 수 없습니다.";
    return;
  }
  const alt = Number(target.alt_deg);
  const minAlt = Number($("minimumSkyAltitude").value || 15);
  if (alt < 0) {
    box.className = "safety-notice danger";
    box.querySelector("span").textContent = `현재 고도 ${alt.toFixed(2)}° — 지평선 아래이므로 관측 계획을 계산하지 않습니다.`;
  } else if (alt < minAlt) {
    box.className = "safety-notice warning";
    box.querySelector("span").textContent = `현재 고도 ${alt.toFixed(2)}° — 최저 분석 고도 ${minAlt.toFixed(1)}°보다 낮습니다.`;
  } else {
    box.className = "safety-notice good";
    box.querySelector("span").textContent = `현재 고도 ${alt.toFixed(2)}° — 전천 배경 조회 가능 범위입니다.`;
  }
  updateReadyState();
}

async function stellariumPing() {
  $("stellariumStatus").textContent = "브라우저에서 Stellarium 연결 확인 중";
  try {
    const response = await fetchLocalStellarium('/api/main/status');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    await response.json();
    $("stellariumStatus").textContent = "Stellarium 연결 정상 · 브라우저 직접 연결";
  } catch (error) {
    $("stellariumStatus").textContent = `연결 실패: ${error.message} · ${stellariumCorsHint()}`;
  }
}

async function importStellariumTarget(reference = false) {
  const statusLabel = $("stellariumStatus");
  statusLabel.textContent = reference ? "기준 천체 정보 가져오는 중" : "선택 천체 정보 가져오는 중";
  try {
    const statusResponse = await fetchLocalStellarium('/api/main/status');
    if (!statusResponse.ok) throw new Error(`status HTTP ${statusResponse.status}`);
    const statusRaw = await statusResponse.json();
    let infoRaw = {};
    try {
      const infoResponse = await fetchLocalStellarium('/api/objects/info?format=json');
      if (infoResponse.ok) infoRaw = await infoResponse.json();
    } catch (_) {
      infoRaw = {};
    }
    const normalizeResponse = await fetch('/api/stellarium/normalize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ info: infoRaw, status: statusRaw }),
    });
    const payload = await normalizeResponse.json();
    if (!normalizeResponse.ok) throw new Error(payload.detail || '가져오기 실패');
    const target = {
      name: payload.name,
      object_type: payload.object_type,
      target_mode: payload.target_mode,
      vmag: payload.vmag,
      vmage: payload.vmage,
      size_deg: payload.size_deg,
      ra_deg: payload.ra_deg,
      dec_deg: payload.dec_deg,
      alt_deg: payload.alt_deg,
      az_deg: payload.az_deg,
      location: payload.location || null,
      time: payload.time || null,
    };
    if (reference) {
      state.referenceTarget = target;
      renderTargetCard(target, true);
      statusLabel.textContent = `기준 천체: ${target.name}`;
    } else {
      state.target = target;
      renderTargetCard(target, false);
      statusLabel.textContent = `관측 대상: ${target.name}`;
    }
  } catch (error) {
    statusLabel.textContent = `가져오기 실패: ${error.message} · ${stellariumCorsHint()}`;
  }
  updateReadyState();
}

function appendFiles(form, field, input) {
  [...input.files].forEach((file) => form.append(field, file));
}

function addMaybe(form, key, value) {
  if (value !== null && value !== undefined && value !== "" && !(typeof value === "number" && !Number.isFinite(value))) {
    form.append(key, String(value));
  }
}

function readLocalProfiles() {
  try {
    const raw = JSON.parse(localStorage.getItem(LOCAL_PROFILE_KEY) || '[]');
    if (!Array.isArray(raw)) return [];
    return raw.filter((item) => item && typeof item === 'object' && typeof item.profile_id === 'string');
  } catch {
    return [];
  }
}

function writeLocalProfiles(profiles) {
  localStorage.setItem(LOCAL_PROFILE_KEY, JSON.stringify(profiles.slice(0, 50)));
}

function saveLocalProfile(profile) {
  const profiles = readLocalProfiles().filter((item) => item.profile_id !== profile.profile_id);
  profiles.unshift(profile);
  writeLocalProfiles(profiles);
}

function selectedProfile() {
  const id = $("equipmentProfile").value;
  return state.profiles.find((item) => item.profile_id === id) || null;
}

function exportSelectedProfile() {
  const profile = selectedProfile();
  if (!profile) {
    $("profileCreateStatus").textContent = '내보낼 장비 프로필을 선택하세요.';
    return;
  }
  const blob = new Blob([JSON.stringify(profile, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `LIGHTT_profile_${(profile.name || profile.profile_id).replace(/[^0-9A-Za-z가-힣._-]+/g, '_')}.json`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  $("profileCreateStatus").textContent = '장비 프로필 JSON을 내보냈습니다.';
}

async function importProfileFile(file) {
  if (!file) return;
  try {
    const raw = JSON.parse(await file.text());
    if (!raw || typeof raw !== 'object' || typeof raw.profile_id !== 'string' || !raw.name) {
      throw new Error('LIGHTT 장비 프로필 JSON 형식이 아닙니다.');
    }
    saveLocalProfile(raw);
    await loadProfiles(raw.profile_id);
    $("profileCreateStatus").textContent = `${raw.name} 프로필을 이 브라우저에 가져왔습니다.`;
  } catch (error) {
    $("profileCreateStatus").textContent = `프로필 가져오기 실패: ${error.message}`;
  } finally {
    $("importProfileFile").value = '';
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
  $("profileCreateStatus").textContent = "기준 영상 측광·포화·장비 응답을 분석하는 중";
  try {
    const response = await fetch("/api/equipment/profiles/create", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "프로필 생성 실패");
    const warningText = Array.isArray(payload.warnings) && payload.warnings.length ? ` · 경고 ${payload.warnings.length}건` : "";
    saveLocalProfile(payload);
    $("profileCreateStatus").textContent = `프로필 생성 완료 · 이 브라우저에 저장 · 신뢰도 ${payload.confidence}${warningText}`;
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
    writeLocalProfiles(readLocalProfiles().filter((item) => item.profile_id !== id));
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
    state.profiles = readLocalProfiles();
    const select = $("equipmentProfile");
    select.innerHTML = "";
    if (!state.profiles.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "이 브라우저에 저장된 장비 프로필 없음";
      select.appendChild(option);
      $("profileRequirement").textContent = "먼저 장비 프로필을 등록하거나 JSON 프로필을 가져오세요.";
      $("profileRequirement").classList.add("warning-text");
    } else {
      state.profiles.forEach((profile) => {
        const option = document.createElement("option");
        option.value = profile.profile_id;
        option.textContent = `${profile.name} · ${profile.confidence}`;
        select.appendChild(option);
      });
      if (selectId && state.profiles.some((item) => item.profile_id === selectId)) select.value = selectId;
      $("profileRequirement").textContent = `${state.profiles.length}개 장비 프로필 · 현재 브라우저에 저장됨`;
      $("profileRequirement").classList.remove("warning-text");
    }
    renderSelectedProfile();
  } catch {
    $("profileRequirement").textContent = "브라우저 장비 프로필을 불러오지 못했습니다.";
  }
  updateReadyState();
}

function renderSelectedProfile() {
  const id = $("equipmentProfile").value;
  const profile = state.profiles.find((item) => item.profile_id === id);
  const box = $("selectedProfileSummary");
  if (!profile) {
    box.innerHTML = "<strong>장비 프로필</strong><span>프로필을 선택하세요.</span>";
    return;
  }
  const calibration = profile.c_sys_quality === "good" ? "Csys 보정" : "상대배경 보정";
  box.innerHTML = "";
  const strong = document.createElement("strong");
  strong.textContent = profile.name;
  const span = document.createElement("span");
  const gainLabel = profile.capture_gain_setting ? ` · ${profile.capture_gain_setting}` : "";
  const noiseLabel = profile.noise_parameters_confirmed ? "센서 물성 확인" : "센서 물성 미확인";
  span.textContent = `${profile.telescope_name || "망원경 미기입"} · ${profile.camera_name || "카메라 미기입"}${gainLabel} · ${calibration} · 기기영점 ${profile.zero_point_quality} · ${noiseLabel}`;
  box.append(strong, span);
}

function updateReadyState() {
  const targetGood = state.target && Number.isFinite(Number(state.target.alt_deg)) && Number(state.target.alt_deg) >= Number($("minimumSkyAltitude").value || 15);
  const ready = Boolean(state.allskyToken && $("equipmentProfile").value && targetGood && !state.analyzing);
  $("analyzeButton").disabled = !ready;
  if (state.analyzing) $("readyStatus").textContent = "분석 수행 중";
  else if (!state.allskyToken) $("readyStatus").textContent = "전천 영상을 입력하세요.";
  else if (!state.target) $("readyStatus").textContent = "Stellarium에서 관측 대상을 가져오세요.";
  else if (!targetGood) $("readyStatus").textContent = "선택 천체가 현재 분석 가능한 고도에 있지 않습니다.";
  else if (!$("equipmentProfile").value) $("readyStatus").textContent = "장비 프로필을 선택하세요.";
  else $("readyStatus").textContent = "분석 준비 완료";
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

async function analyzeSession() {
  if ($("analyzeButton").disabled) return;
  startProgress();
  const form = new FormData();
  form.append("allsky_token", state.allskyToken);
  const allskyFile = $("allsky").files[0];
  if (allskyFile) form.append("allsky", allskyFile);
  const profile = selectedProfile();
  if (!profile) { stopProgress(); return; }
  form.append("profile_id", profile.profile_id);
  form.append("profile_json", JSON.stringify(profile));
  const target = state.target;
  form.append("target_name", target.name || "선택 천체");
  form.append("target_object_type", target.object_type || "unknown");
  form.append("target_mode", target.target_mode || "extended");
  addMaybe(form, "target_vmag", target.vmag);
  addMaybe(form, "target_vmage", target.vmage);
  addMaybe(form, "target_size_deg", target.size_deg);
  addMaybe(form, "target_alt_deg", target.alt_deg);
  addMaybe(form, "target_az_deg", target.az_deg);
  addMaybe(form, "target_ra_deg", target.ra_deg);
  addMaybe(form, "target_dec_deg", target.dec_deg);
  addMaybe(form, "target_time_utc", target.time?.utc);
  addMaybe(form, "target_time_local", target.time?.local);
  addMaybe(form, "target_latitude", target.location?.latitude);
  addMaybe(form, "target_longitude", target.location?.longitude);
  addMaybe(form, "allsky_exposure_sec", valueOrNull("allskyExposure"));
  addMaybe(form, "allsky_bias_offset_adu", valueOrNull("allskyBiasOffset"));
  form.append("target_snr", $("targetSnr").value);
  form.append("min_sub_exposure_sec", $("minExposure").value);
  form.append("max_sub_exposure_sec", $("maxExposure").value);
  form.append("tracking_limit_sec", $("trackingLimit").value);
  form.append("background_limit_fraction", $("backgroundLimit").value);
  form.append("saturation_safety_fraction", $("saturationSafety").value);
  form.append("stack_efficiency", $("stackEfficiency").value);
  form.append("max_frames", $("maxFrames").value);
  form.append("frame_overhead_sec", $("frameOverhead").value);
  form.append("effective_pixels", $("effectivePixels").value);
  form.append("minimum_sky_altitude_deg", $("minimumSkyAltitude").value);
  form.append("az_bins", $("azBins").value);
  form.append("alt_bins", $("altBins").value);
  addMaybe(form, "manual_target_mag", valueOrNull("manualTargetMag"));
  addMaybe(form, "manual_surface_brightness_mag_arcsec2", valueOrNull("manualSurfaceBrightness"));
  appendFiles(form, "allsky_bias", $("allskyBias"));
  appendFiles(form, "allsky_dark", $("allskyDark"));
  appendFiles(form, "allsky_flat", $("allskyFlat"));

  try {
    const response = await fetch("/api/session/analyze", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "분석 실패");
    renderResult(payload);
  } catch (error) {
    $("readyStatus").textContent = `분석 실패: ${error.message}`;
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
  const p = document.createElement("p"); p.textContent = description;
  const a = document.createElement("a"); a.href = url; a.download = ""; a.className = "secondary small"; a.textContent = "이미지 저장";
  copy.append(h, p, a); article.append(img, copy); return article;
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
  $("confidenceBox").textContent = `결과 신뢰도: ${confidenceLabel(result.confidence)} · ${validityLabel(result.validity)}`;
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

function wireEvents() {
  document.querySelectorAll("[data-scroll]").forEach((button) => button.addEventListener("click", () => $(button.dataset.scroll).scrollIntoView({ behavior: "smooth" })));
  $("openProfileManager").addEventListener("click", () => { $("profileManager").classList.remove("hidden"); $("profileManager").scrollIntoView({ behavior: "smooth" }); });
  $("closeProfileManager").addEventListener("click", () => $("profileManager").classList.add("hidden"));
  $("profileScopeFile").addEventListener("change", () => inspectReferenceScope($("profileScopeFile").files[0]));
  $("profileAllskyFile").addEventListener("change", () => { $("profileAllskyName").textContent = $("profileAllskyFile").files[0]?.name || "선택하지 않음"; });
  $("allsky").addEventListener("change", () => inspectAllsky($("allsky").files[0]));
  $("stellariumPing").addEventListener("click", stellariumPing);
  $("stellariumPingDaily").addEventListener("click", stellariumPing);
  $("importReferenceTarget").addEventListener("click", () => importStellariumTarget(true));
  $("setStellariumReferenceTime").addEventListener("click", setStellariumReferenceTime);
  $("setStellariumNow").addEventListener("click", setStellariumNow);
  $("importTarget").addEventListener("click", () => importStellariumTarget(false));
  $("createProfile").addEventListener("click", createEquipmentProfile);
  $("deleteProfile").addEventListener("click", deleteSelectedProfile);
  $("exportProfile").addEventListener("click", exportSelectedProfile);
  $("importProfile").addEventListener("click", () => $("importProfileFile").click());
  $("importProfileFile").addEventListener("change", () => importProfileFile($("importProfileFile").files[0]));
  $("equipmentProfile").addEventListener("change", () => { renderSelectedProfile(); updateReadyState(); });
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
  $("closeModal").addEventListener("click", () => $("imageModal").classList.add("hidden"));
  $("imageModal").addEventListener("click", (event) => { if (event.target === $("imageModal")) $("imageModal").classList.add("hidden"); });
}

window.addEventListener("DOMContentLoaded", async () => {
  wireEvents();
  await checkServer();
  await loadProfiles();
  updateReadyState();
});
