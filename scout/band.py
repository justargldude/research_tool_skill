"""Stage [4] Band assignment + hysteresis (v6 spec §5).

Pure function, deterministic, zero token, zero network.
Handles band assignment, symmetric hysteresis logic, flip logging,
dead flag forcing, and per-domain sample size (N) knobs.
"""

from __future__ import annotations

from typing import Any

from scout.config import BandConfig, BandThreshold

DEFAULT_DOMAIN_N: int = 30

DOMAIN_N: dict[str, int] = {
    "web-frameworks": 50,
    "ai-ml": 50,
    "databases": 40,
    "devsecops": 30,
    "compilers": 30,
    "data-engineering": 40,
    "cloud-native": 40,
    "default": DEFAULT_DOMAIN_N,
}


def domain_n(domain: str) -> int:
    """Trả về cỡ mẫu tối thiểu N theo domain (spec §4). Domain lạ -> default >= 1."""
    if not isinstance(domain, str):
        return DEFAULT_DOMAIN_N
    return DOMAIN_N.get(domain.strip().lower(), DEFAULT_DOMAIN_N)


n_for_domain = domain_n
min_n_for_domain = domain_n
min_batch_for_domain = domain_n

N_BY_DOMAIN = DOMAIN_N
MIN_N_BY_DOMAIN = DOMAIN_N
DOMAIN_SAMPLE_N = DOMAIN_N


def _get_raw_band(score: float, sorted_bands: list[BandThreshold]) -> str:
    """Gán band thuần túy theo ngưỡng, biên inclusive."""
    for b in sorted_bands:
        if score >= b.min_percentile:
            return b.name
    return sorted_bands[-1].name


def assign_band(
    score: float,
    prev_band: str | None,
    cfg: BandConfig,
    dead: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Gán band xếp hạng với logic hysteresis đối xứng (spec §5).

    - dead=True: ép band 'unverified' bất kể score.
    - prev_band=None: gán theo ngưỡng, biên inclusive (score == min_percentile -> vào band).
    - Hysteresis đối xứng: flip sang band mới chỉ khi score >= min_percentile(band mới) + hysteresis_points.
    - Giữ prev_band nếu score vẫn nằm trong prev_band hoặc chưa đủ điều kiện flip.
    """
    sorted_bands = sorted(cfg.bands, key=lambda b: b.min_percentile, reverse=True)
    unverified_name = sorted_bands[-1].name

    if dead:
        if prev_band is not None and prev_band != unverified_name:
            return unverified_name, [{"from": prev_band, "to": unverified_name}]
        return unverified_name, []

    if prev_band is None:
        return _get_raw_band(score, sorted_bands), []

    threshold_map = {b.name: b.min_percentile for b in sorted_bands}
    prev_thresh = threshold_map.get(prev_band, 0.0)

    # Kiểm tra nếu score nằm trong prev_band
    # Tìm band trên liền kề của prev_band nếu có
    higher_bands = [b for b in sorted_bands if b.min_percentile > prev_thresh]
    prev_upper_bound = higher_bands[-1].min_percentile if higher_bands else float("inf")

    if prev_thresh <= score < prev_upper_bound:
        return prev_band, []

    # Score di chuyển lên phía trên prev_band
    if score >= prev_upper_bound:
        for b in sorted_bands:
            if b.min_percentile > prev_thresh:
                if score >= b.min_percentile + cfg.hysteresis_points:
                    return b.name, [{"from": prev_band, "to": b.name}]
        # Chưa đạt ngưỡng hysteresis của band cao hơn -> giữ nguyên prev_band
        return prev_band, []

    # Score di chuyển xuống phía dưới prev_band
    if score < prev_thresh:
        for b in sorted_bands:
            if b.min_percentile < prev_thresh:
                if score >= b.min_percentile + cfg.hysteresis_points:
                    return b.name, [{"from": prev_band, "to": b.name}]
        # Chưa đạt ngưỡng hysteresis của band thấp hơn -> giữ nguyên prev_band
        return prev_band, []

    return prev_band, []
