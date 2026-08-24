from __future__ import annotations

import asyncio
import ctypes
import gc
import json
import math
import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from lightt import __version__
from lightt.io import SUPPORTED_EXTENSIONS, infer_intensity_domain, load_image
from lightt.models import AnalysisSettings, CalibrationSet
from lightt.equipment import EquipmentProfile, create_equipment_profile, delete_profile, list_profiles, load_profile
from lightt.session import run_session_analysis
from lightt.photometry import propose_extended_rois
from lightt.pipeline import run_analysis
from lightt.stellarium import first_recursive, normalize_selected_object
from lightt.stellarium import import_selected as stellarium_import_selected
from lightt.stellarium import ping as stellarium_ping_service
from lightt.stellarium import set_simulation_time as stellarium_set_time_service
from lightt.time_utils import image_observation_time_utc
from lightt.visualization import save_scope_preview

ROOT = Path(__file__).resolve().parent
UPLOAD_ROOT = ROOT / "uploads"
RESULT_ROOT = ROOT / "results"
PROFILE_ROOT = ROOT / "profiles"
STATIC_ROOT = ROOT / "static"
UPLOAD_ROOT.mkdir(exist_ok=True)
RESULT_ROOT.mkdir(exist_ok=True)
PROFILE_ROOT.mkdir(exist_ok=True)

MAX_UPLOAD_BYTES = int(os.environ.get("LIGHTT_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
MAX_REQUEST_BYTES = int(os.environ.get("LIGHTT_MAX_REQUEST_BYTES", str(3 * 1024**3)))
MAX_IMAGE_PIXELS = int(os.environ.get("LIGHTT_MAX_IMAGE_PIXELS", str(120_000_000)))
ANALYSIS_CONCURRENCY = max(1, int(os.environ.get("LIGHTT_ANALYSIS_CONCURRENCY", "1")))
ANALYSIS_SEMAPHORE = asyncio.Semaphore(ANALYSIS_CONCURRENCY)
TOKEN_PATTERN = re.compile(r"^[a-f0-9]{24}$")
SERVER_INSTANCE_ID = uuid.uuid4().hex[:12]
SERVER_STARTED_AT = time.time()


def _release_memory() -> None:
    """Return large temporary NumPy/Matplotlib allocations to the OS when possible."""
    gc.collect()
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim = getattr(libc, "malloc_trim", None)
        if trim is not None:
            trim(0)
    except Exception:
        pass


def _load_profile_or_snapshot(profile_id: str, profile_snapshot_json: str | None) -> tuple[EquipmentProfile, bool]:
    """Load a server profile, falling back to a browser-cached snapshot after a Render restart."""
    try:
        return load_profile(PROFILE_ROOT, profile_id), False
    except ValueError as original:
        if not profile_snapshot_json:
            raise original
        if len(profile_snapshot_json) > 500_000:
            raise ValueError("브라우저 장비 프로필 백업이 너무 큽니다.") from original
        try:
            raw = json.loads(profile_snapshot_json)
            if not isinstance(raw, dict):
                raise ValueError("프로필 백업 형식이 올바르지 않습니다.")
            profile = EquipmentProfile.from_dict(raw)
        except Exception as exc:
            raise ValueError("브라우저 장비 프로필 백업을 읽을 수 없습니다.") from exc
        if profile.profile_id != profile_id:
            raise ValueError("브라우저 장비 프로필 백업 ID가 현재 선택과 다릅니다.")
        if not profile.name:
            raise ValueError("브라우저 장비 프로필 백업이 불완전합니다.")
        return profile, True

@asynccontextmanager
async def lifespan(_: FastAPI):
    _cleanup_old_jobs(max_age_hours=48.0)
    yield


app = FastAPI(title="NØXIS", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")
app.mount("/results", StaticFiles(directory=RESULT_ROOT), name="results")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
        "script-src 'self'; connect-src 'self' http://127.0.0.1:* http://localhost:*"
    )
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "version": __version__,
        "analysis_concurrency": ANALYSIS_CONCURRENCY,
        "instance_id": SERVER_INSTANCE_ID,
        "uptime_sec": round(time.time() - SERVER_STARTED_AT, 1),
        "pid": os.getpid(),
    }


@app.get("/version")
def version() -> dict[str, str]:
    return {"version": __version__, "name": "NØXIS astrophotography planner"}


@app.post("/api/stellarium/normalize")
def stellarium_normalize(
    payload: Annotated[dict[str, object], Body()],
) -> dict[str, object]:
    """Normalize browser-fetched Stellarium data without proxying localhost through Render."""
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    if not info or "raw_text" in info:
        fallback = first_recursive(
            status,
            ["selectioninfo", "selectionInfo", "selectedObject", "objectInfo"],
        )
        info = fallback if isinstance(fallback, dict) else {}
    if not info:
        raise HTTPException(
            status_code=422,
            detail="선택 천체 정보를 읽지 못했습니다. Stellarium에서 천체를 선택하세요.",
        )
    result = normalize_selected_object(info, status)
    result.update({"ok": True, "warnings": [], "server_instance_id": SERVER_INSTANCE_ID})
    return result


@app.get("/api/stellarium/ping")
def stellarium_ping(base_url: str = "http://127.0.0.1:8090") -> dict[str, object]:
    try:
        return stellarium_ping_service(base_url)
    except Exception as exc:
        return {"ok": False, "base_url": base_url, "message": str(exc)}


@app.get("/api/stellarium/import")
def stellarium_import(base_url: str = "http://127.0.0.1:8090") -> dict[str, object]:
    try:
        return stellarium_import_selected(base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Stellarium 연결 또는 선택 천체 가져오기에 실패했습니다: {type(exc).__name__}",
        ) from exc


@app.post("/api/stellarium/set-time")
def stellarium_set_time(
    base_url: Annotated[str, Form()] = "http://127.0.0.1:8090",
    observation_time_utc: Annotated[str, Form()] = "",
    pause: Annotated[bool, Form()] = True,
) -> dict[str, object]:
    if not observation_time_utc.strip():
        raise HTTPException(status_code=422, detail="기준 영상 촬영시각을 확인할 수 없습니다.")
    try:
        result = stellarium_set_time_service(base_url, observation_time_utc, pause=pause)
        if not result.get("ok"):
            raise HTTPException(status_code=502, detail="Stellarium이 시각 변경 요청을 확인하지 않았습니다.")
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Stellarium 시각 설정에 실패했습니다: {type(exc).__name__}",
        ) from exc


def _clean_filename(name: str | None, fallback: str) -> str:
    candidate = Path(name or fallback).name
    safe = "".join(ch for ch in candidate if ch.isalnum() or ch in "._-()")
    return safe or fallback


class UploadBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def add(self, size: int) -> None:
        self.used += size
        if self.used > self.limit:
            raise HTTPException(
                status_code=413,
                detail=f"한 번의 요청에서 올린 전체 파일이 {self.limit/1024**3:.1f} GiB 제한을 초과했습니다.",
            )


async def _save_upload(
    upload: UploadFile,
    directory: Path,
    prefix: str,
    budget: UploadBudget,
) -> Path:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 파일 형식입니다: {suffix or '(없음)'}")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prefix}_{_clean_filename(upload.filename, prefix + suffix)}"
    total = 0
    try:
        with path.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="파일 하나가 허용 크기를 초과했습니다.")
                budget.add(len(chunk))
                handle.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if total == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="빈 파일은 분석할 수 없습니다.")
    return path


async def _save_optional_uploads(
    uploads: list[UploadFile] | None,
    directory: Path,
    prefix: str,
    budget: UploadBudget,
    limit: int = 30,
) -> list[Path]:
    if not uploads:
        return []
    if len(uploads) > limit:
        raise HTTPException(status_code=400, detail=f"{prefix} 보정 프레임은 최대 {limit}개입니다.")
    return [
        await _save_upload(upload, directory, f"{prefix}_{index:02d}", budget)
        for index, upload in enumerate(uploads)
    ]


def _finite_range(name: str, value: float, low: float, high: float) -> float:
    if not math.isfinite(value) or not low <= value <= high:
        raise HTTPException(status_code=422, detail=f"{name}은(는) {low}~{high} 범위여야 합니다.")
    return value


def _cleanup_old_jobs(max_age_hours: float = 48.0) -> None:
    cutoff = time.time() - max_age_hours * 3600
    for root in (UPLOAD_ROOT, RESULT_ROOT):
        for path in root.iterdir():
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue


def _validate_pixels(width: int, height: int) -> None:
    pixels = width * height
    if pixels > MAX_IMAGE_PIXELS:
        raise HTTPException(
            status_code=413,
            detail=f"영상이 {pixels/1_000_000:.1f}MP로 서버 제한 {MAX_IMAGE_PIXELS/1_000_000:.1f}MP를 초과합니다.",
        )


def _inspect_path(token: str, role: str) -> Path:
    if not TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(status_code=400, detail="미리보기 토큰 형식이 올바르지 않습니다.")
    directory = UPLOAD_ROOT / f"inspect_{token}"
    candidates = [path for path in directory.glob(f"{role}_*") if path.is_file()]
    if len(candidates) != 1:
        raise HTTPException(status_code=410, detail="미리보기 파일이 만료되었습니다. 파일을 다시 선택하세요.")
    return candidates[0]


@app.post("/api/inspect")
async def inspect_image(
    file: Annotated[UploadFile, File(...)],
    role: Annotated[str, Form()] = "image",
    sensor_clip_adu: Annotated[float | None, Form()] = None,
    safety: Annotated[float, Form()] = 0.80,
) -> JSONResponse:
    _cleanup_old_jobs(max_age_hours=12.0)
    safe_role = role if role in {"allsky", "scope", "image"} else "image"
    token = uuid.uuid4().hex[:24]
    job_dir = UPLOAD_ROOT / f"inspect_{token}"
    budget = UploadBudget(MAX_REQUEST_BYTES)
    try:
        path = await _save_upload(file, job_dir, safe_role, budget)
        frame = await run_in_threadpool(load_image, path)
        _validate_pixels(frame.metadata.width, frame.metadata.height)
        domain = None
        inspect_warnings: list[str] = []
        try:
            domain = await run_in_threadpool(infer_intensity_domain, frame, sensor_clip_adu, safety)
        except Exception as exc:
            # Metadata/preview are still useful even when detector-domain inference is not.
            inspect_warnings.append(f"ADU 범위 판정 생략: {type(exc).__name__}: {str(exc)[:180]}")

        preview_id = "preview_" + uuid.uuid4().hex[:12]
        preview_dir = RESULT_ROOT / preview_id
        preview_dir.mkdir(parents=True, exist_ok=False)
        preview_path = preview_dir / f"{safe_role}_preview.png"
        preview_url: str | None = None
        preview_warning: str | None = None
        try:
            await run_in_threadpool(save_scope_preview, frame.intensity, preview_path)
            preview_url = f"/results/{preview_id}/{preview_path.name}"
        except Exception as exc:
            preview_warning = f"서버 미리보기 생략: {type(exc).__name__}"
            inspect_warnings.append(preview_warning)
        suggested_rois = None
        if safe_role == "scope":
            try:
                target_roi, background_roi = await run_in_threadpool(propose_extended_rois, frame.intensity)
                suggested_rois = {
                    "target": {
                        "x": target_roi["x"] / frame.metadata.width,
                        "y": target_roi["y"] / frame.metadata.height,
                        "w": target_roi["w"] / frame.metadata.width,
                        "h": target_roi["h"] / frame.metadata.height,
                    },
                    "background": {
                        "x": background_roi["x"] / frame.metadata.width,
                        "y": background_roi["y"] / frame.metadata.height,
                        "w": background_roi["w"] / frame.metadata.width,
                        "h": background_roi["h"] / frame.metadata.height,
                    },
                }
            except Exception:
                suggested_rois = None
        response_payload = {
            "metadata": asdict(frame.metadata),
            "capture_time_utc": image_observation_time_utc(frame.metadata.date_obs, frame.metadata.source_type),
            "domain": asdict(domain) if domain is not None else None,
            "preview_url": preview_url,
            "preview_warning": preview_warning,
            "warnings": inspect_warnings,
            "upload_token": token,
            "suggested_rois": suggested_rois,
        }
        # Drop the full-resolution detector array before returning to keep the small
        # Render instance from accumulating image memory between inspections.
        del frame
        _release_memory()
        return JSONResponse(response_payload)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        message = str(exc).strip().replace("\n", " ")[:240]
        raise HTTPException(
            status_code=400,
            detail=f"영상을 읽을 수 없습니다: {type(exc).__name__}{(': ' + message) if message else ''}",
        ) from exc
    finally:
        _release_memory()


async def _resolve_main_file(
    upload: UploadFile | None,
    token: str | None,
    role: str,
    job_dir: Path,
    budget: UploadBudget,
) -> Path:
    if token:
        return _inspect_path(token, role)
    if upload is None:
        raise HTTPException(status_code=400, detail=f"{role} 영상 파일이 필요합니다.")
    return await _save_upload(upload, job_dir, role, budget)


def _profile_reference_source(profile_id: str, role: str) -> Path | None:
    # Validate the id through the normal profile loader first.
    load_profile(PROFILE_ROOT, profile_id)
    directory = PROFILE_ROOT / profile_id
    stem = "reference_scope" if role == "scope" else "reference_allsky"
    candidates = sorted(path for path in directory.glob(stem + ".*") if path.is_file())
    return candidates[0] if candidates else None


def _profile_preview_url(profile_id: str, role: str) -> str | None:
    try:
        source = _profile_reference_source(profile_id, role)
    except ValueError:
        return None
    if source is None:
        return None
    return f"/api/equipment/profiles/{profile_id}/preview/{role}"


@app.get("/api/equipment/profiles")
def equipment_profiles() -> dict[str, object]:
    profiles = list_profiles(PROFILE_ROOT)
    return {
        "profiles": [
            {
                "profile_id": item.profile_id,
                "name": item.name,
                "created_at": item.created_at,
                "telescope_name": item.telescope_name,
                "camera_name": item.camera_name,
                "filter_name": item.filter_name,
                "capture_gain_setting": item.capture_gain_setting,
                "binning": item.binning,
                "confidence": item.confidence,
                "noise_parameters_confirmed": item.noise_parameters_confirmed,
                "zero_point_quality": item.zero_point_quality,
                "c_sys_quality": item.c_sys_quality,
                "warnings": item.warnings,
                "scope_preview_url": _profile_preview_url(item.profile_id, "scope"),
                "allsky_preview_url": _profile_preview_url(item.profile_id, "allsky"),
            }
            for item in profiles
        ]
    }


@app.get("/api/equipment/profiles/{profile_id}")
def equipment_profile_detail(profile_id: str) -> dict[str, object]:
    try:
        return load_profile(PROFILE_ROOT, profile_id).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/equipment/profiles/{profile_id}/preview/{role}")
async def equipment_profile_preview(profile_id: str, role: str) -> FileResponse:
    if role not in {"scope", "allsky"}:
        raise HTTPException(status_code=404, detail="미리보기 종류가 올바르지 않습니다.")
    try:
        source = _profile_reference_source(profile_id, role)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=404, detail="등록된 기준 영상이 없습니다.")
    directory = PROFILE_ROOT / profile_id
    preview_path = directory / f"preview_{role}.png"
    try:
        if (not preview_path.exists()) or preview_path.stat().st_mtime < source.stat().st_mtime:
            frame = await run_in_threadpool(load_image, source)
            _validate_pixels(frame.metadata.width, frame.metadata.height)
            await run_in_threadpool(save_scope_preview, frame.intensity, preview_path)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"등록 영상 미리보기 실패: {type(exc).__name__}") from exc
    return FileResponse(preview_path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.delete("/api/equipment/profiles/{profile_id}")
def equipment_profile_delete(profile_id: str) -> dict[str, object]:
    try:
        delete_profile(PROFILE_ROOT, profile_id)
        return {"ok": True, "profile_id": profile_id}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/equipment/profiles/create")
async def equipment_profile_create(
    scope: Annotated[UploadFile, File(...)],
    reference_allsky: Annotated[UploadFile | None, File()] = None,
    scope_bias: Annotated[list[UploadFile] | None, File()] = None,
    scope_dark: Annotated[list[UploadFile] | None, File()] = None,
    scope_flat: Annotated[list[UploadFile] | None, File()] = None,
    allsky_bias: Annotated[list[UploadFile] | None, File()] = None,
    allsky_dark: Annotated[list[UploadFile] | None, File()] = None,
    allsky_flat: Annotated[list[UploadFile] | None, File()] = None,
    profile_name: Annotated[str, Form()] = "",
    telescope_name: Annotated[str, Form()] = "",
    camera_name: Annotated[str, Form()] = "",
    filter_name: Annotated[str, Form()] = "",
    capture_gain_setting: Annotated[str, Form()] = "",
    binning: Annotated[str, Form()] = "",
    gain_e_per_adu: Annotated[float, Form()] = 1.0,
    read_noise_e: Annotated[float, Form()] = 3.0,
    dark_current_e_per_pix_sec: Annotated[float, Form()] = 0.0,
    noise_parameters_confirmed: Annotated[bool, Form()] = False,
    bias_offset_adu: Annotated[float | None, Form()] = None,
    sensor_clip_adu: Annotated[float | None, Form()] = None,
    pixel_scale_arcsec: Annotated[float | None, Form()] = None,
    extinction_k_mag_per_airmass: Annotated[float, Form()] = 0.20,
    scope_exposure_sec: Annotated[float | None, Form()] = None,
    reference_allsky_exposure_sec: Annotated[float | None, Form()] = None,
    reference_target_name: Annotated[str, Form()] = "기준 천체",
    reference_target_type: Annotated[str, Form()] = "unknown",
    reference_target_mode: Annotated[str, Form()] = "extended",
    reference_target_mag: Annotated[float | None, Form()] = None,
    reference_target_size_deg: Annotated[float | None, Form()] = None,
    reference_target_alt_deg: Annotated[float | None, Form()] = None,
    reference_target_az_deg: Annotated[float | None, Form()] = None,
    reference_target_ra_deg: Annotated[float | None, Form()] = None,
    reference_target_dec_deg: Annotated[float | None, Form()] = None,
    reference_target_time_utc: Annotated[str | None, Form()] = None,
    reference_target_time_local: Annotated[str | None, Form()] = None,
    reference_target_latitude: Annotated[float | None, Form()] = None,
    reference_target_longitude: Annotated[float | None, Form()] = None,
) -> JSONResponse:
    _finite_range("Gain", gain_e_per_adu, 0.000001, 1000)
    _finite_range("읽기잡음", read_noise_e, 0, 1000)
    _finite_range("암전류", dark_current_e_per_pix_sec, 0, 10000)
    if bias_offset_adu is not None:
        _finite_range("망원경 Bias/offset", bias_offset_adu, -1e9, 1e9)
    _finite_range("대기소광 계수", extinction_k_mag_per_airmass, 0, 5)
    if sensor_clip_adu is not None:
        _finite_range("센서 포화 ADU", sensor_clip_adu, 0.000001, 1e12)
    if pixel_scale_arcsec is not None:
        _finite_range("pixel scale", pixel_scale_arcsec, 0.001, 10000)
    if scope_exposure_sec is not None:
        _finite_range("기준 망원경 노출", scope_exposure_sec, 0.000001, 86400)
    if reference_allsky_exposure_sec is not None:
        _finite_range("기준 전천 노출", reference_allsky_exposure_sec, 0.000001, 86400)
    if reference_target_mode not in {"point", "extended"}:
        raise HTTPException(status_code=422, detail="기준 천체 유형이 올바르지 않습니다.")
    for label, value, low, high in (
        ("기준 천체 고도", reference_target_alt_deg, -90, 90),
        ("기준 천체 방위각", reference_target_az_deg, 0, 360),
        ("기준 천체 적경", reference_target_ra_deg, 0, 360),
        ("기준 천체 적위", reference_target_dec_deg, -90, 90),
        ("기준 Stellarium 위도", reference_target_latitude, -90, 90),
        ("기준 Stellarium 경도", reference_target_longitude, -180, 180),
    ):
        if value is not None:
            _finite_range(label, value, low, high)
    request_id = f"profile_{uuid.uuid4().hex}"
    job_dir = UPLOAD_ROOT / request_id
    budget = UploadBudget(MAX_REQUEST_BYTES)
    try:
        scope_path = await _save_upload(scope, job_dir, "scope", budget)
        ref_allsky_path = None
        if reference_allsky is not None:
            ref_allsky_path = await _save_upload(reference_allsky, job_dir, "allsky", budget)
        scope_cal = CalibrationSet(
            bias_paths=await _save_optional_uploads(scope_bias, job_dir / "scope_bias", "bias", budget),
            dark_paths=await _save_optional_uploads(scope_dark, job_dir / "scope_dark", "dark", budget),
            flat_paths=await _save_optional_uploads(scope_flat, job_dir / "scope_flat", "flat", budget),
        )
        allsky_cal = CalibrationSet(
            bias_paths=await _save_optional_uploads(allsky_bias, job_dir / "allsky_bias", "bias", budget),
            dark_paths=await _save_optional_uploads(allsky_dark, job_dir / "allsky_dark", "dark", budget),
            flat_paths=await _save_optional_uploads(allsky_flat, job_dir / "allsky_flat", "flat", budget),
        )
        target = {
            "name": reference_target_name[:200],
            "object_type": reference_target_type[:100],
            "target_mode": reference_target_mode,
            "vmag": reference_target_mag,
            "size_deg": reference_target_size_deg,
            "alt_deg": reference_target_alt_deg,
            "az_deg": reference_target_az_deg,
            "ra_deg": reference_target_ra_deg,
            "dec_deg": reference_target_dec_deg,
            "observation_time_utc": reference_target_time_utc,
            "observation_time_local": reference_target_time_local,
            "latitude": reference_target_latitude,
            "longitude": reference_target_longitude,
        }
        async with ANALYSIS_SEMAPHORE:
            profile = await run_in_threadpool(
                create_equipment_profile,
                profile_root=PROFILE_ROOT,
                profile_name=profile_name,
                scope_path=scope_path,
                reference_target=target,
                project_root=ROOT,
                telescope_name=telescope_name,
                camera_name=camera_name,
                filter_name=filter_name,
                capture_gain_setting=capture_gain_setting,
                binning=binning,
                gain_e_per_adu=gain_e_per_adu,
                read_noise_e=read_noise_e,
                dark_current_e_per_pix_sec=dark_current_e_per_pix_sec,
                noise_parameters_confirmed=noise_parameters_confirmed,
                bias_offset_adu=bias_offset_adu,
                sensor_clip_adu=sensor_clip_adu,
                pixel_scale_arcsec=pixel_scale_arcsec,
                extinction_k_mag_per_airmass=extinction_k_mag_per_airmass,
                scope_exposure_sec=scope_exposure_sec,
                scope_calibration=scope_cal,
                reference_allsky_path=ref_allsky_path,
                reference_allsky_exposure_sec=reference_allsky_exposure_sec,
                allsky_calibration=allsky_cal,
                result_root=RESULT_ROOT,
            )
        return JSONResponse(profile.to_dict())
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"장비 프로필 생성 중 오류가 발생했습니다: {type(exc).__name__}") from exc
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        _release_memory()


@app.post("/api/session/analyze")
async def session_analyze(
    allsky: Annotated[UploadFile | None, File()] = None,
    allsky_token: Annotated[str | None, Form()] = None,
    allsky_bias: Annotated[list[UploadFile] | None, File()] = None,
    allsky_dark: Annotated[list[UploadFile] | None, File()] = None,
    allsky_flat: Annotated[list[UploadFile] | None, File()] = None,
    profile_id: Annotated[str, Form()] = "",
    profile_snapshot_json: Annotated[str | None, Form()] = None,
    target_name: Annotated[str, Form()] = "",
    target_object_type: Annotated[str, Form()] = "unknown",
    target_mode: Annotated[str, Form()] = "extended",
    target_vmag: Annotated[float | None, Form()] = None,
    target_vmage: Annotated[float | None, Form()] = None,
    target_size_deg: Annotated[float | None, Form()] = None,
    target_alt_deg: Annotated[float | None, Form()] = None,
    target_az_deg: Annotated[float | None, Form()] = None,
    target_ra_deg: Annotated[float | None, Form()] = None,
    target_dec_deg: Annotated[float | None, Form()] = None,
    target_time_utc: Annotated[str | None, Form()] = None,
    target_time_local: Annotated[str | None, Form()] = None,
    target_latitude: Annotated[float | None, Form()] = None,
    target_longitude: Annotated[float | None, Form()] = None,
    allsky_exposure_sec: Annotated[float | None, Form()] = None,
    allsky_bias_offset_adu: Annotated[float | None, Form()] = None,
    target_snr: Annotated[float, Form()] = 100.0,
    min_sub_exposure_sec: Annotated[float, Form()] = 1.0,
    max_sub_exposure_sec: Annotated[float, Form()] = 600.0,
    tracking_limit_sec: Annotated[float, Form()] = 0.0,
    background_limit_fraction: Annotated[float, Form()] = 0.30,
    saturation_safety_fraction: Annotated[float, Form()] = 0.80,
    stack_efficiency: Annotated[float, Form()] = 0.90,
    max_frames: Annotated[int, Form()] = 2000,
    frame_overhead_sec: Annotated[float, Form()] = 2.0,
    effective_pixels: Annotated[int, Form()] = 100,
    minimum_sky_altitude_deg: Annotated[float, Form()] = 15.0,
    az_bins: Annotated[int, Form()] = 72,
    alt_bins: Annotated[int, Form()] = 18,
    manual_target_mag: Annotated[float | None, Form()] = None,
    manual_surface_brightness_mag_arcsec2: Annotated[float | None, Form()] = None,
) -> JSONResponse:
    if not profile_id:
        raise HTTPException(status_code=422, detail="장비 프로필을 선택하세요.")
    try:
        profile, profile_recovered = _load_profile_or_snapshot(profile_id, profile_snapshot_json)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if target_mode not in {"point", "extended"}:
        raise HTTPException(status_code=422, detail="Stellarium 대상 유형이 올바르지 않습니다.")
    if target_alt_deg is None or target_az_deg is None:
        raise HTTPException(status_code=422, detail="Stellarium에서 선택 천체의 고도와 방위각을 가져오세요.")
    _finite_range("대상 고도", target_alt_deg, -90, 90)
    _finite_range("대상 방위각", target_az_deg, 0, 360)
    if target_latitude is not None:
        _finite_range("Stellarium 위도", target_latitude, -90, 90)
    if target_longitude is not None:
        _finite_range("Stellarium 경도", target_longitude, -180, 180)
    _finite_range("목표 SNR", target_snr, 0.1, 10000)
    _finite_range("최소 단일노출", min_sub_exposure_sec, 0.001, 86400)
    _finite_range("최대 단일노출", max_sub_exposure_sec, min_sub_exposure_sec, 86400)
    _finite_range("배경 제한비율", background_limit_fraction, 0.01, 0.95)
    _finite_range("포화 안전비율", saturation_safety_fraction, 0.05, 0.99)
    _finite_range("스택 효율", stack_efficiency, 0.3, 1.0)
    _finite_range("최저 분석 고도", minimum_sky_altitude_deg, 0, 45)
    if tracking_limit_sec:
        _finite_range("추적 한계", tracking_limit_sec, 0.001, 86400)
    if allsky_exposure_sec is not None:
        _finite_range("전천 노출시간", allsky_exposure_sec, 0.000001, 86400)
    if allsky_bias_offset_adu is not None:
        _finite_range("전천 Bias/offset", allsky_bias_offset_adu, -1e9, 1e9)
    if not 1 <= effective_pixels <= 100000:
        raise HTTPException(status_code=422, detail="유효 측정 픽셀 수는 1~100,000 범위여야 합니다.")
    if not 1 <= max_frames <= 100000:
        raise HTTPException(status_code=422, detail="최대 프레임 수가 허용 범위를 벗어납니다.")
    if not 12 <= az_bins <= 144 or not 6 <= alt_bins <= 45:
        raise HTTPException(status_code=422, detail="전천지도 bin 수가 허용 범위를 벗어납니다.")

    request_id = f"session_{uuid.uuid4().hex}"
    job_dir = UPLOAD_ROOT / request_id
    budget = UploadBudget(MAX_REQUEST_BYTES)
    try:
        allsky_path = await _resolve_main_file(allsky, allsky_token, "allsky", job_dir, budget)
        allsky_cal = CalibrationSet(
            bias_paths=await _save_optional_uploads(allsky_bias, job_dir / "allsky_bias", "bias", budget),
            dark_paths=await _save_optional_uploads(allsky_dark, job_dir / "allsky_dark", "dark", budget),
            flat_paths=await _save_optional_uploads(allsky_flat, job_dir / "allsky_flat", "flat", budget),
        )
        target = {
            "name": target_name[:200],
            "object_type": target_object_type[:100],
            "target_mode": target_mode,
            "vmag": target_vmag,
            "vmage": target_vmage,
            "size_deg": target_size_deg,
            "alt_deg": target_alt_deg,
            "az_deg": target_az_deg,
            "ra_deg": target_ra_deg,
            "dec_deg": target_dec_deg,
            "observation_time_utc": target_time_utc,
            "observation_time_local": target_time_local,
            "latitude": target_latitude,
            "longitude": target_longitude,
        }
        async with ANALYSIS_SEMAPHORE:
            result = await run_in_threadpool(
                run_session_analysis,
                allsky_path=allsky_path,
                profile=profile,
                target_payload=target,
                project_root=ROOT,
                result_root=RESULT_ROOT,
                allsky_calibration=allsky_cal,
                allsky_exposure_sec=allsky_exposure_sec,
                allsky_bias_offset_adu=allsky_bias_offset_adu,
                target_snr=target_snr,
                min_sub_exposure_sec=min_sub_exposure_sec,
                max_sub_exposure_sec=max_sub_exposure_sec,
                tracking_limit_sec=tracking_limit_sec,
                background_limit_fraction=background_limit_fraction,
                saturation_safety_fraction=saturation_safety_fraction,
                stack_efficiency=stack_efficiency,
                max_frames=max_frames,
                frame_overhead_sec=frame_overhead_sec,
                effective_pixels=effective_pixels,
                minimum_sky_altitude_deg=minimum_sky_altitude_deg,
                az_bins=az_bins,
                alt_bins=alt_bins,
                manual_target_mag=manual_target_mag,
                manual_surface_brightness_mag_arcsec2=manual_surface_brightness_mag_arcsec2,
            )
        if profile_recovered:
            result.setdefault("runtime", {})["profile_recovered_from_browser"] = True
            result.setdefault("warnings", []).append("Render 서버 재시작 후 브라우저에 저장된 장비 프로필을 자동 복구했습니다.")
        result.setdefault("runtime", {})["server_instance_id"] = SERVER_INSTANCE_ID
        return JSONResponse(result)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"관측 계획 분석 중 오류가 발생했습니다: {type(exc).__name__}") from exc
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        _release_memory()



@app.post("/api/analyze")
async def analyze(
    allsky: Annotated[UploadFile | None, File()] = None,
    scope: Annotated[UploadFile | None, File()] = None,
    allsky_token: Annotated[str | None, Form()] = None,
    scope_token: Annotated[str | None, Form()] = None,
    allsky_bias: Annotated[list[UploadFile] | None, File()] = None,
    allsky_dark: Annotated[list[UploadFile] | None, File()] = None,
    allsky_flat: Annotated[list[UploadFile] | None, File()] = None,
    scope_bias: Annotated[list[UploadFile] | None, File()] = None,
    scope_dark: Annotated[list[UploadFile] | None, File()] = None,
    scope_flat: Annotated[list[UploadFile] | None, File()] = None,
    current_exposure_sec: Annotated[float, Form()] = 180.0,
    exposure_mode: Annotated[str, Form()] = "auto",
    target_snr: Annotated[float, Form()] = 100.0,
    target_mode: Annotated[str, Form()] = "extended",
    target_name: Annotated[str, Form()] = "",
    gain_e_per_adu: Annotated[float, Form()] = 1.0,
    read_noise_e: Annotated[float, Form()] = 3.0,
    noise_parameters_confirmed: Annotated[bool, Form()] = False,
    dark_current_e_per_pix_sec: Annotated[float, Form()] = 0.0,
    bias_offset_adu: Annotated[float, Form()] = 0.0,
    sensor_clip_adu: Annotated[float | None, Form()] = None,
    saturation_safety_fraction: Annotated[float, Form()] = 0.80,
    background_limit_fraction: Annotated[float, Form()] = 0.30,
    max_sub_exposure_sec: Annotated[float, Form()] = 600.0,
    min_sub_exposure_sec: Annotated[float, Form()] = 1.0,
    tracking_limit_sec: Annotated[float, Form()] = 0.0,
    frame_overhead_sec: Annotated[float, Form()] = 2.0,
    stack_efficiency: Annotated[float, Form()] = 0.90,
    max_recommended_frames: Annotated[int, Form()] = 2000,
    saturation_policy: Annotated[str, Form()] = "balanced",
    allow_unverified_saturation: Annotated[bool, Form()] = False,
    smoothing_pixels: Annotated[int, Form()] = 100,
    target_roi_json: Annotated[str | None, Form()] = None,
    background_roi_json: Annotated[str | None, Form()] = None,
    auto_roi: Annotated[bool, Form()] = True,
    auto_roi_confirmed: Annotated[bool, Form()] = False,
    target_coordinate_mode: Annotated[str, Form()] = "altaz",
    target_alt_deg: Annotated[float, Form()] = 45.0,
    target_az_deg: Annotated[float, Form()] = 180.0,
    target_ra_deg: Annotated[float | None, Form()] = None,
    target_dec_deg: Annotated[float | None, Form()] = None,
    allsky_exposure_sec: Annotated[float | None, Form()] = None,
    latitude: Annotated[float, Form()] = 37.70,
    longitude: Annotated[float, Form()] = 128.26,
    height_m: Annotated[float, Form()] = 50.0,
    observation_time: Annotated[str, Form()] = "",
    timezone: Annotated[str, Form()] = "KST",
    az_bins: Annotated[int, Form()] = 72,
    alt_bins: Annotated[int, Form()] = 18,
    minimum_sky_altitude_deg: Annotated[float, Form()] = 15.0,
    beginner_mode: Annotated[bool, Form()] = True,
) -> JSONResponse:
    _cleanup_old_jobs()
    request_id = f"upload_{uuid.uuid4().hex}"
    job_dir = UPLOAD_ROOT / request_id
    budget = UploadBudget(MAX_REQUEST_BYTES)
    try:
        _finite_range("현재 노출시간", current_exposure_sec, 0.0001, 86400)
        _finite_range("목표 SNR", target_snr, 0.1, 10000)
        _finite_range("gain", gain_e_per_adu, 0.000001, 1000)
        _finite_range("읽기잡음", read_noise_e, 0, 1000)
        _finite_range("dark current", dark_current_e_per_pix_sec, 0, 10000)
        _finite_range("포화 안전비율", saturation_safety_fraction, 0.05, 0.99)
        _finite_range("배경 제한비율", background_limit_fraction, 0.01, 0.95)
        _finite_range("스택 효율", stack_efficiency, 0.3, 1.0)
        _finite_range("최저 하늘 고도", minimum_sky_altitude_deg, 0, 45)
        _finite_range("최대 단일노출", max_sub_exposure_sec, 0.01, 86400)
        _finite_range("최소 단일노출", min_sub_exposure_sec, 0.001, max_sub_exposure_sec)
        _finite_range("위도", latitude, -90, 90)
        _finite_range("경도", longitude, -180, 180)
        _finite_range("관측지 고도", height_m, -500, 10000)
        _finite_range("프레임 오버헤드", frame_overhead_sec, 0, 3600)
        if tracking_limit_sec != 0:
            _finite_range("추적 한계", tracking_limit_sec, 0.001, 86400)
        if sensor_clip_adu is not None:
            _finite_range("센서 포화값", sensor_clip_adu, 0.000001, 1e12)
        if allsky_exposure_sec is not None:
            _finite_range("전천 영상 노출시간", allsky_exposure_sec, 0.000001, 86400)
        if not 1 <= smoothing_pixels <= 100_000:
            raise HTTPException(status_code=422, detail="측정 영역 픽셀 수는 1~100,000 범위여야 합니다.")
        if not 1 <= max_recommended_frames <= 100_000:
            raise HTTPException(status_code=422, detail="최대 권장 장수가 허용 범위를 벗어납니다.")
        if target_coordinate_mode not in {"altaz", "radec"}:
            raise HTTPException(status_code=422, detail="목표 좌표 모드가 올바르지 않습니다.")
        if target_coordinate_mode == "altaz":
            _finite_range("목표 고도", target_alt_deg, -90, 90)
            _finite_range("목표 방위각", target_az_deg, 0, 360)
        else:
            if target_ra_deg is None or target_dec_deg is None:
                raise HTTPException(status_code=422, detail="RA/Dec 모드에서는 적경과 적위가 모두 필요합니다.")
            _finite_range("적경", target_ra_deg, 0, 360)
            _finite_range("적위", target_dec_deg, -90, 90)
            if not observation_time.strip():
                raise HTTPException(status_code=422, detail="RA/Dec 모드에서는 관측 시각이 필요합니다.")
        if timezone not in {"KST", "UTC"}:
            raise HTTPException(status_code=422, detail="시간대가 올바르지 않습니다.")
        if exposure_mode not in {"auto", "manual", "header"}:
            raise HTTPException(status_code=422, detail="노출시간 모드가 올바르지 않습니다.")
        if target_mode not in {"extended", "point"}:
            raise HTTPException(status_code=422, detail="대상 모드가 올바르지 않습니다.")
        if saturation_policy not in {"preserve_stars", "balanced", "target_priority"}:
            raise HTTPException(status_code=422, detail="포화 정책이 올바르지 않습니다.")
        if not 12 <= az_bins <= 144 or not 6 <= alt_bins <= 45:
            raise HTTPException(status_code=422, detail="전천지도 bin 수가 허용 범위를 벗어납니다.")

        allsky_path = await _resolve_main_file(allsky, allsky_token, "allsky", job_dir, budget)
        scope_path = await _resolve_main_file(scope, scope_token, "scope", job_dir, budget)
        allsky_cal = CalibrationSet(
            bias_paths=await _save_optional_uploads(allsky_bias, job_dir / "allsky_bias", "bias", budget),
            dark_paths=await _save_optional_uploads(allsky_dark, job_dir / "allsky_dark", "dark", budget),
            flat_paths=await _save_optional_uploads(allsky_flat, job_dir / "allsky_flat", "flat", budget),
        )
        scope_cal = CalibrationSet(
            bias_paths=await _save_optional_uploads(scope_bias, job_dir / "scope_bias", "bias", budget),
            dark_paths=await _save_optional_uploads(scope_dark, job_dir / "scope_dark", "dark", budget),
            flat_paths=await _save_optional_uploads(scope_flat, job_dir / "scope_flat", "flat", budget),
        )
        settings = AnalysisSettings(
            current_exposure_sec=current_exposure_sec,
            exposure_mode=exposure_mode,  # type: ignore[arg-type]
            target_snr=target_snr,
            target_mode=target_mode,  # type: ignore[arg-type]
            target_name=target_name[:200],
            gain_e_per_adu=gain_e_per_adu,
            read_noise_e=read_noise_e,
            noise_parameters_confirmed=noise_parameters_confirmed,
            dark_current_e_per_pix_sec=dark_current_e_per_pix_sec,
            bias_offset_adu=bias_offset_adu,
            sensor_clip_adu=sensor_clip_adu,
            saturation_safety_fraction=saturation_safety_fraction,
            background_limit_fraction=background_limit_fraction,
            max_sub_exposure_sec=max_sub_exposure_sec,
            min_sub_exposure_sec=min_sub_exposure_sec,
            tracking_limit_sec=tracking_limit_sec,
            frame_overhead_sec=frame_overhead_sec,
            stack_efficiency=stack_efficiency,
            max_recommended_frames=max_recommended_frames,
            saturation_policy=saturation_policy,  # type: ignore[arg-type]
            allow_unverified_saturation=allow_unverified_saturation,
            smoothing_pixels=smoothing_pixels,
            target_roi_json=target_roi_json,
            background_roi_json=background_roi_json,
            auto_roi=auto_roi,
            auto_roi_confirmed=auto_roi_confirmed,
            target_coordinate_mode=target_coordinate_mode,  # type: ignore[arg-type]
            target_alt_deg=target_alt_deg,
            target_az_deg=target_az_deg,
            target_ra_deg=target_ra_deg,
            target_dec_deg=target_dec_deg,
            allsky_exposure_sec=allsky_exposure_sec,
            latitude=latitude,
            longitude=longitude,
            height_m=height_m,
            observation_time=observation_time,
            timezone=timezone,
            az_bins=az_bins,
            alt_bins=alt_bins,
            minimum_sky_altitude_deg=minimum_sky_altitude_deg,
            beginner_mode=beginner_mode,
        )
        async with ANALYSIS_SEMAPHORE:
            result = await run_in_threadpool(
                run_analysis,
                allsky_path=allsky_path,
                scope_path=scope_path,
                settings=settings,
                allsky_calibration=allsky_cal,
                scope_calibration=scope_cal,
                project_root=ROOT,
                result_root=RESULT_ROOT,
            )
        return JSONResponse(result.to_dict())
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"분석 중 내부 오류가 발생했습니다: {type(exc).__name__}",
        ) from exc
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        _release_memory()


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8010, reload=False)
