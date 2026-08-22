from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from scipy import ndimage

from .models import SignalMeasurement, StarMeasurement
from .plotting import plot_text


def _reduce(image: np.ndarray, max_dim: int) -> tuple[np.ndarray, float, float]:
    height, width = image.shape
    scale = max(height / max_dim, width / max_dim, 1.0)
    if scale <= 1:
        return image, 1.0, 1.0
    out_h, out_w = max(1, int(round(height / scale))), max(1, int(round(width / scale)))
    reduced = ndimage.zoom(image, (out_h / height, out_w / width), order=1, prefilter=False)
    return reduced, out_w / width, out_h / height


def _reduce_mask_any(mask: np.ndarray, max_dim: int) -> np.ndarray:
    height, width = mask.shape
    factor = max(1, int(math.ceil(max(height / max_dim, width / max_dim))))
    if factor == 1:
        return mask.astype(bool, copy=False)
    out_h, out_w = math.ceil(height / factor), math.ceil(width / factor)
    padded = np.zeros((out_h * factor, out_w * factor), dtype=bool)
    padded[:height, :width] = mask
    reduced = padded.reshape(out_h, factor, out_w, factor).any(axis=(1, 3))
    return np.asarray(reduced, dtype=bool)


def _display_array(image: np.ndarray) -> np.ndarray:
    finite = image[np.isfinite(image)]
    if finite.size == 0:
        return np.zeros(image.shape, dtype=np.uint8)
    low, high = np.percentile(finite, [0.5, 99.7])
    scaled = np.clip((image.astype(np.float32) - low) / max(high - low, 1e-9), 0, 1)
    np.sqrt(scaled, out=scaled)
    return np.asarray(np.round(scaled * 255), dtype=np.uint8)


def save_scope_preview(image: np.ndarray, output: Path, max_dim: int = 1800) -> None:
    """Save a lightweight preview without allocating a full interpolation buffer.

    Inspection runs on small Render instances too, so a strided reduction is preferred
    here over scipy zoom. Scientific analysis continues to use the original array.
    """
    if image.ndim != 2:
        raise ValueError("미리보기 입력은 2차원 영상이어야 합니다.")
    height, width = image.shape
    step = max(1, int(math.ceil(max(height / max_dim, width / max_dim))))
    reduced = image[::step, ::step]
    Image.fromarray(_display_array(reduced), mode="L").save(output, optimize=True)


def save_scope_overlay(
    image: np.ndarray,
    measurement: SignalMeasurement,
    stars: list[StarMeasurement],
    output: Path,
    max_dim: int = 1800,
) -> None:
    reduced, sx, sy = _reduce(image, max_dim)
    fig, ax = plt.subplots(figsize=(12, 8), constrained_layout=True)
    ax.imshow(_display_array(reduced), cmap="gray", origin="upper")
    for star in stars[:150]:
        ax.plot(
            star.x * sx,
            star.y * sy,
            marker="o",
            markersize=4,
            markerfacecolor="none",
            markeredgecolor="red" if star.saturated else "lime",
            alpha=0.7,
        )
    if measurement.target_roi:
        roi = measurement.target_roi
        ax.add_patch(
            plt.Rectangle(
                (roi["x"] * sx, roi["y"] * sy),
                roi["w"] * sx,
                roi["h"] * sy,
                fill=False,
                linewidth=2,
                edgecolor="cyan",
                label=plot_text("대상 영역", "Target ROI"),
            )
        )
    if measurement.background_roi:
        roi = measurement.background_roi
        ax.add_patch(
            plt.Rectangle(
                (roi["x"] * sx, roi["y"] * sy),
                roi["w"] * sx,
                roi["h"] * sy,
                fill=False,
                linewidth=2,
                edgecolor="yellow",
                label=plot_text("배경 영역", "Background ROI"),
            )
        )
    if measurement.target_roi or measurement.background_roi:
        ax.legend(loc="upper right")
    ax.set_title(plot_text("망원경 측정 영역 · 초록=유효 별, 빨강=포화 별, 청록=대상, 노랑=배경", "Telescope measurement overlay · green=valid stars, red=saturated, cyan=target, yellow=background"))
    ax.set_axis_off()
    fig.savefig(output, dpi=150)
    plt.close(fig)


def save_adu_histogram(
    image: np.ndarray,
    output: Path,
    *,
    sensor_clip_adu: float | None = None,
    saturation_threshold_adu: float | None = None,
) -> None:
    finite = image[np.isfinite(image)]
    if finite.size == 0:
        finite = np.array([0.0])
    if finite.size > 1_000_000:
        step = max(1, finite.size // 1_000_000)
        finite = finite[::step]
    p001, p9995 = np.percentile(finite, [0.1, 99.95])
    if p9995 <= p001:
        p9995 = p001 + 1.0
    fig, axes = plt.subplots(1, 2, figsize=(14, 5), constrained_layout=True)
    axes[0].hist(np.clip(finite, p001, p9995), bins=180)
    axes[0].axvline(float(np.median(finite)), linestyle="--", label=plot_text("중앙값", "Median"))
    axes[0].set_title(plot_text("대부분의 픽셀 ADU 분포", "Main pixel ADU distribution"))
    axes[0].set_xlabel("ADU")
    axes[0].set_ylabel(plot_text("픽셀 수", "Pixel count"))
    axes[0].set_yscale("log")
    axes[0].legend(loc="upper right")

    tail_low = float(np.percentile(finite, 98.0))
    tail_high = max(float(np.max(finite)), tail_low + 1.0)
    tail = finite[finite >= tail_low]
    axes[1].hist(tail, bins=120)
    if saturation_threshold_adu is not None:
        axes[1].axvline(saturation_threshold_adu, linestyle="--", label=plot_text("포화 안전 판정선", "Saturation safety threshold"))
    if sensor_clip_adu is not None:
        axes[1].axvline(sensor_clip_adu, linestyle=":", label=plot_text("센서 포화값", "Sensor clipping ADU"))
    axes[1].set_xlim(tail_low, tail_high * 1.01)
    axes[1].set_title(plot_text("밝은 픽셀 확대", "Bright-tail detail"))
    axes[1].set_xlabel("ADU")
    axes[1].set_ylabel(plot_text("픽셀 수", "Pixel count"))
    axes[1].set_yscale("log")
    if sensor_clip_adu is not None or saturation_threshold_adu is not None:
        axes[1].legend(loc="upper right")
    fig.savefig(output, dpi=160)
    plt.close(fig)


def save_saturation_diagnostic(
    image: np.ndarray,
    threshold_adu: float,
    output: Path,
    max_dim: int = 1600,
) -> None:
    saturated_full = np.isfinite(image) & (image >= threshold_adu)
    saturated = _reduce_mask_any(saturated_full, max_dim)
    reduced, _, _ = _reduce(image, max_dim)
    display = _display_array(reduced)
    # Make dimensions exactly match if ceil-based mask pooling differs by one pixel.
    if saturated.shape != display.shape:
        saturated = ndimage.zoom(
            saturated.astype(float),
            (display.shape[0] / saturated.shape[0], display.shape[1] / saturated.shape[1]),
            order=0,
            prefilter=False,
        ) > 0.5
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), constrained_layout=True)
    axes[0].imshow(display, cmap="gray", origin="upper")
    if np.any(saturated):
        overlay = np.zeros((*saturated.shape, 4), dtype=np.float32)
        overlay[..., 0] = 1.0
        overlay[..., 3] = saturated.astype(np.float32) * 0.78
        axes[0].imshow(overlay, origin="upper")
    axes[0].set_title(plot_text("포화 위치 오버레이 · 빨간색", "Saturated-pixel overlay · red"))
    axes[0].set_axis_off()
    axes[1].imshow(saturated, cmap="gray", origin="upper")
    axes[1].set_title(
        f"{threshold_adu:.1f} ADU 이상 · 원본 {int(np.count_nonzero(saturated_full)):,}픽셀"
    )
    axes[1].set_axis_off()
    fig.savefig(output, dpi=160)
    plt.close(fig)


def save_exposure_snr_curve(
    exposure_seconds: np.ndarray,
    snr_values: np.ndarray,
    output: Path,
    *,
    current_exposure_sec: float,
    current_snr: float,
    target_snr: float,
    recommended_exposure_sec: float | None,
    practical_upper_sec: float | None,
) -> None:
    valid = (
        np.isfinite(exposure_seconds)
        & np.isfinite(snr_values)
        & (exposure_seconds > 0)
        & (snr_values >= 0)
    )
    x, y = exposure_seconds[valid], snr_values[valid]
    fig, ax = plt.subplots(figsize=(10, 5.5), constrained_layout=True)
    if x.size:
        ax.plot(x, y, linewidth=2, label=plot_text("예상 한 장 SNR", "Predicted single-frame SNR"))
        ax.set_xscale("log")
    ax.axhline(target_snr, linestyle="--", label=plot_text("목표 SNR", "Target SNR"))
    ax.scatter([current_exposure_sec], [current_snr], s=60, label=plot_text("현재 시험 영상", "Current test frame"))
    if recommended_exposure_sec is not None:
        ax.axvline(recommended_exposure_sec, linestyle="--", label=plot_text("권장 단일노출", "Recommended sub-exposure"))
    if practical_upper_sec is not None:
        ax.axvline(practical_upper_sec, linestyle=":", label=plot_text("안전 상한", "Practical safety limit"))
    ax.set_xlabel(plot_text("한 장 노출시간 (초, 로그 눈금)", "Sub-exposure time (s, log scale)"))
    ax.set_ylabel(plot_text("예상 SNR", "Predicted SNR"))
    ax.set_title(plot_text("노출시간에 따른 한 장 SNR 예상", "Predicted single-frame SNR vs exposure"))
    ax.grid(alpha=0.25)
    ax.legend(loc="best")
    fig.savefig(output, dpi=160)
    plt.close(fig)
