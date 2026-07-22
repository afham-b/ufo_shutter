import argparse
import csv
import math
import os
import sys
import subprocess

import cv2
import numpy as np

DURATIONS = {
    "durations1": [10,20,30,50,75,100,150,200,250,260,270,280,287,290,300,310,500,750,1000,1500,2000,2500,3000,4000],
    "durations2": [10,12,15,17,20,22,24,26,28,30,32,34,36,38,40,42,45,50,60,75,80,85,100,150],
    "durations3": [10,11,12,13,14,15,16,17,18,19,20,22,24,26,28,30],
    "durations4": [75,76,77,78,79,80,81,82,83,84,85],
}

# ---------------- Helpers ----------------

def moving_average(x, w):
    if w <= 1:
        return x
    x = np.asarray(x, dtype=float)
    kernel = np.ones(w, dtype=float) / w
    return np.convolve(x, kernel, mode="same")


def frame_to_gray(frame):
    if frame is None:
        return None
    if len(frame.shape) == 2:
        return frame
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def quick_metric(gray, topk=80):
    g = gray.astype(np.float32, copy=False).reshape(-1)
    med = float(np.median(g))
    k = int(max(1, min(topk, g.size)))
    top = np.partition(g, -k)[-k:]
    return float(np.mean(top) - med)


def center_from_frame(gray, topk=100):
    h, w = gray.shape
    flat = gray.reshape(-1)
    k = int(max(1, min(topk, flat.size)))
    idx = np.argpartition(flat, -k)[-k:]
    ys = idx // w
    xs = idx - ys * w
    weights = flat[idx].astype(np.float64)
    wsum = float(np.sum(weights))
    if wsum <= 0:
        maxLoc = np.unravel_index(int(np.argmax(flat)), (h, w))
        return float(maxLoc[1]), float(maxLoc[0])
    cx = float(np.sum(xs * weights) / wsum)
    cy = float(np.sum(ys * weights) / wsum)
    return cx, cy


def get_video_info(path):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or math.isnan(fps) or fps <= 1:
        fps = 110.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return w, h, fps


def iter_frames_cv2(path):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {path}")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = frame_to_gray(frame)
        if gray is None:
            continue
        yield gray
    cap.release()


def iter_frames_ffmpeg(path, width, height):
    frame_size = width * height
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if buf is None or len(buf) < frame_size:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape((height, width))
            yield frame
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()


def make_r2_map(w, h, cx, cy):
    yy, xx = np.mgrid[0:h, 0:w]
    return (xx - cx) ** 2 + (yy - cy) ** 2


def build_masks(h, w, cx, cy, opts):
    r2 = make_r2_map(w, h, cx, cy)
    ap_mask = r2 <= (opts.r_ap ** 2)
    bg_mask = (r2 >= (opts.r_bg_in ** 2)) & (r2 <= (opts.r_bg_out ** 2))
    ann_mask = (r2 >= (opts.r_in ** 2)) & (r2 <= (opts.r_out ** 2))
    return ap_mask, bg_mask, ann_mask


def aperture_metric(gray, ap_mask, bg_mask, sat_thr=250):
    ap_pix = gray[ap_mask].astype(np.float32)
    bg_pix = gray[bg_mask].astype(np.float32)

    bg_pix = bg_pix[bg_pix < sat_thr]
    if bg_pix.size < 20:
        bg = float(np.median(gray.reshape(-1)))
    else:
        bg = float(np.median(bg_pix))

    ap_use = ap_pix[ap_pix < sat_thr]
    if ap_use.size < 10:
        return 0.0, 1.0

    flux = float(np.sum(ap_use - bg))
    sat_frac = float(np.mean(ap_pix >= sat_thr)) if ap_pix.size else 0.0
    return flux, sat_frac


def annulus_metric(gray, ann_mask, sat_thr=250):
    pix = gray[ann_mask].astype(np.float32)
    pix_use = pix[pix < sat_thr]
    if pix_use.size < 10:
        return 0.0, 1.0
    bg = float(np.median(pix_use))
    flux = float(np.sum(pix_use - bg))
    sat_frac = float(np.mean(pix >= sat_thr)) if pix.size else 0.0
    return flux, sat_frac

def ring_metric(gray, ring_mask, bg_mask, sat_thr=250):
    """
    Signal from a ring (annulus) and background from an outer ring.
    Useful when the core is saturated but outer wings are not.
    """
    ring_pix = gray[ring_mask].astype(np.float32)
    bg_pix = gray[bg_mask].astype(np.float32)

    bg_pix = bg_pix[bg_pix < sat_thr]
    if bg_pix.size < 20:
        bg = float(np.median(gray.reshape(-1)))
    else:
        bg = float(np.median(bg_pix))

    ring_use = ring_pix[ring_pix < sat_thr]
    if ring_use.size < 10:
        return 0.0, 1.0

    flux = float(np.sum(ring_use - bg))
    sat_frac = float(np.mean(ring_pix >= sat_thr)) if ring_pix.size else 0.0
    return flux, sat_frac

def topk_cap_metric(gray, topk=200, sat_cap=240):
    g = gray.astype(np.float32, copy=False).reshape(-1)
    med = float(np.median(g))
    k = int(max(1, min(topk, g.size)))
    top = np.partition(g, -k)[-k:]
    top = np.clip(top, 0, sat_cap)
    flux = float(np.sum(top) - k * med)
    sat_frac = float(np.mean(g >= sat_cap)) if g.size else 0.0
    return flux, sat_frac


def estimate_baseline_full(sm):
    lo = float(np.percentile(sm, 5))
    hi = float(np.percentile(sm, 95))
    swing = max(0.0, hi - lo)
    if swing <= 1e-9:
        # Sparse-pulse videos can have p95 ~ baseline; use upper-tail fallback.
        baseline = lo
        full = float(np.percentile(sm, 99.9))
        if full <= baseline:
            full = float(np.max(sm))
        if full <= baseline:
            full = baseline + 1e-9
        return baseline, full

    thr = lo + 0.2 * swing
    open_mask = sm > thr

    if np.any(~open_mask):
        baseline = float(np.median(sm[~open_mask]))
    else:
        baseline = lo

    if np.any(open_mask):
        full = float(np.percentile(sm[open_mask], 95))
    else:
        full = hi

    if full <= baseline:
        full = hi
    return baseline, full


def hysteresis_states(signal, thr_open, thr_close):
    s = np.asarray(signal, dtype=float)
    out = np.zeros(len(s), dtype=bool)
    state = False
    for i, v in enumerate(s):
        if not state and v >= thr_open:
            state = True
        elif state and v <= thr_close:
            state = False
        out[i] = state
    return out


def find_segments(mask, min_len=3):
    segs = []
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        if (not v) and start is not None:
            end = i - 1
            if end - start + 1 >= min_len:
                segs.append((start, end))
            start = None
    if start is not None:
        end = len(mask) - 1
        if end - start + 1 >= min_len:
            segs.append((start, end))
    return segs


def merge_close_segments(segs, gap_frames):
    if not segs:
        return []
    segs = sorted(segs)
    out = [list(segs[0])]
    for s, e in segs[1:]:
        if s - out[-1][1] <= gap_frames:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def expected_pulse_starts_s(commanded_ms, gap_s, first_start_s=0.0):
    starts = [first_start_s]
    t = first_start_s
    for i in range(1, len(commanded_ms)):
        t += (commanded_ms[i - 1] / 1000.0) + gap_s
        starts.append(t)
    return np.array(starts, dtype=float)


def assign_segments_by_expected(segs, sm, fps, commanded_ms, gap_s,
                                first_start_s=None, max_assign_s=None):
    """
    Assign each detected segment to the closest expected pulse start.
    Returns a list of length len(commanded_ms) with (start, end) or None.
    """
    n = len(commanded_ms)
    out = [None] * n
    if not segs:
        return out

    segs = sorted(segs, key=lambda se: se[0])

    if first_start_s is None:
        exp_offsets = expected_pulse_starts_s(commanded_ms, gap_s, first_start_s=0.0)
        m = min(len(segs), n)
        candidates = []
        for i in range(m):
            candidates.append((segs[i][0] / fps) - exp_offsets[i])
        first_start_s = float(np.median(candidates)) if candidates else (segs[0][0] / fps)

    exp_starts = expected_pulse_starts_s(commanded_ms, gap_s, first_start_s=first_start_s)

    if max_assign_s is None:
        min_spacing = gap_s + (min(commanded_ms) / 1000.0)
        max_assign_s = 0.6 * min_spacing

    baseline = float(np.percentile(sm, 10))
    best = {}  # pulse_idx -> (auc, (s, e))
    for s, e in segs:
        seg_start_s = s / fps
        j = int(np.argmin(np.abs(exp_starts - seg_start_s)))
        if abs(exp_starts[j] - seg_start_s) > max_assign_s:
            continue

        seg = sm[s:e + 1]
        auc_bs_frames = float(np.sum(np.maximum(seg - baseline, 0.0)))
        auc_bs_s = auc_bs_frames / fps
        if (j not in best) or (auc_bs_s > best[j][0]):
            best[j] = (auc_bs_s, (s, e))

    for j in range(n):
        if j in best:
            out[j] = best[j][1]
    return out


def choose_pulse_segments(segs, sm, fps, commanded_ms, gap_s):
    """
    Prefer one-to-one ordering when segment count matches pulse count.
    Fall back to schedule-aware assignment when there are extra/missing segments.
    """
    n = len(commanded_ms)
    if not segs:
        return [None] * n

    segs = sorted(segs, key=lambda se: se[0])
    if len(segs) == n:
        return list(segs)

    return assign_segments_by_expected(segs, sm, fps, commanded_ms, gap_s)


def infer_leading_missing_assignment(segs, fps, commanded_ms, gap_s, video_duration_s):
    """
    Map detected segments to a contiguous suffix of the command list.
    This handles runs where early short commands produced no visible light.
    """
    n = len(commanded_ms)
    k = len(segs)
    if k == 0 or k > n:
        return None

    segs = sorted(segs, key=lambda se: se[0])
    obs = np.array([s / fps for s, _ in segs], dtype=float)
    offsets = expected_pulse_starts_s(commanded_ms, gap_s, first_start_s=0.0)

    best = None
    best_score = None
    for lead in range(n - k + 1):
        idx = np.arange(lead, lead + k)
        exp = offsets[idx]
        anchor = float(np.median(obs - exp))
        resid = obs - (anchor + exp)
        rmse = float(np.sqrt(np.mean(resid ** 2)))

        predicted_end = anchor + offsets[-1] + (commanded_ms[-1] / 1000.0)
        start_penalty = max(0.0, -anchor - 0.25) * 5.0
        end_penalty = max(0.0, predicted_end - video_duration_s) * 50.0
        score = rmse + start_penalty + end_penalty

        if (best_score is None) or (score < best_score):
            mapped = [None] * n
            for seg_i, cmd_i in enumerate(idx):
                mapped[cmd_i] = segs[seg_i]
            best = mapped
            best_score = score

    return best


def segment_eff_ms(norm, fps, s, e):
    seg = norm[s:e + 1]
    return 1000.0 * float(np.sum(seg)) / fps


def segment_peak_norm(norm, s, e):
    seg = norm[s:e + 1]
    return float(np.max(seg)) if seg.size else 0.0


def is_useful_segment(norm, fps, s, e, opts):
    eff_ms = segment_eff_ms(norm, fps, s, e)
    peak_norm = segment_peak_norm(norm, s, e)
    return (
        eff_ms >= opts.useful_eff_ms and
        peak_norm >= opts.useful_peak_norm
    )


def crossing_time(t, y, thr, direction="rising", start_idx=0, end_idx=None):
    if end_idx is None:
        end_idx = len(y) - 1
    if end_idx <= start_idx:
        return None

    if direction == "rising":
        for i in range(start_idx + 1, end_idx + 1):
            if y[i - 1] < thr <= y[i]:
                y0, y1 = y[i - 1], y[i]
                if y1 == y0:
                    return t[i]
                frac = (thr - y0) / (y1 - y0)
                return t[i - 1] + frac * (t[i] - t[i - 1])
    else:
        for i in range(start_idx + 1, end_idx + 1):
            if y[i - 1] > thr >= y[i]:
                y0, y1 = y[i - 1], y[i]
                if y1 == y0:
                    return t[i]
                frac = (y0 - thr) / (y0 - y1)
                return t[i - 1] + frac * (t[i] - t[i - 1])
    return None


def parse_durations(args):
    if args.durations_list:
        parts = [p.strip() for p in args.durations_list.split(",") if p.strip()]
        return [int(p) for p in parts]
    return DURATIONS[args.durations]


def compute_metrics(video_path, opts, center=None, masks=None, use_ffmpeg=False):
    if center is None or masks is None:
        raise RuntimeError("compute_metrics requires center and masks")

    w, h, fps = get_video_info(video_path)
    if not fps or math.isnan(fps) or fps <= 1:
        fps = 110.0
        print(f"[WARN] FPS metadata missing for {video_path}. Using 110 fps fallback.")

    ap_mask, bg_mask, ann_mask = masks
    metrics = []
    sat_fracs = []

    frame_iter = iter_frames_ffmpeg(video_path, w, h) if use_ffmpeg else iter_frames_cv2(video_path)

    for gray in frame_iter:
        if gray is None or gray.size == 0:
            metrics.append(0.0)
            sat_fracs.append(0.0)
            continue

        if opts.mode == "APERTURE":
            m, sf = aperture_metric(gray, ap_mask, bg_mask, sat_thr=opts.sat_thr)
        elif opts.mode == "ANNULUS":
            m, sf = annulus_metric(gray, ann_mask, sat_thr=opts.sat_thr)
        elif opts.mode == "RING":
            m, sf = ring_metric(gray, ann_mask, bg_mask, sat_thr=opts.sat_thr)
        elif opts.mode == "TOPKCAP":
            m, sf = topk_cap_metric(gray, topk=opts.topk, sat_cap=opts.sat_cap)
        else:
            raise ValueError("Unknown MODE")

        metrics.append(m)
        sat_fracs.append(sf)

    metrics = np.asarray(metrics, dtype=float)
    sat_fracs = np.asarray(sat_fracs, dtype=float)
    return metrics, sat_fracs, fps


def center_from_video(video_path, opts, use_ffmpeg=False, max_frames=2000):
    w, h, _ = get_video_info(video_path)
    frame_iter = iter_frames_ffmpeg(video_path, w, h) if use_ffmpeg else iter_frames_cv2(video_path)

    best_metric = -1.0
    best_gray = None
    count = 0

    for gray in frame_iter:
        if gray is None or gray.size == 0:
            continue
        m = quick_metric(gray, topk=opts.quick_topk)
        if m > best_metric:
            best_metric = m
            best_gray = gray.copy()
        count += 1
        if max_frames and count >= max_frames:
            break

    if best_gray is None:
        raise RuntimeError(f"No frames read from {video_path}.")

    cx, cy = center_from_frame(best_gray, topk=opts.center_topk)
    return (cx, cy), (h, w)


def compute_reference_level(video_path, opts, center, masks, which="baseline", use_ffmpeg=False):
    metrics, _, _ = compute_metrics(video_path, opts, center=center, masks=masks, use_ffmpeg=use_ffmpeg)
    sm = moving_average(metrics, opts.smooth)
    if which == "baseline":
        return float(np.percentile(sm, opts.baseline_percentile))
    return float(np.percentile(sm, opts.full_percentile))


# ---------------- Main ----------------

def analyze_video(video_path, commanded_ms, gap_s, out_dir, opts,
                 center=None, masks=None, baseline_ref=None, full_ref=None, use_ffmpeg=False):
    metrics, sat_fracs, fps = compute_metrics(
        video_path, opts, center=center, masks=masks, use_ffmpeg=use_ffmpeg
    )
    sm = moving_average(metrics, opts.smooth)

    baseline, full = estimate_baseline_full(sm)
    if baseline_ref is not None:
        baseline = baseline_ref
    if full_ref is not None:
        full = full_ref

    if full <= baseline:
        baseline, full = estimate_baseline_full(sm)

    swing = max(1e-9, full - baseline)
    norm = (sm - baseline) / swing
    norm = np.clip(norm, 0.0, 1.2)

    print(f"{os.path.basename(video_path)}: frames={len(metrics)} fps={fps:.2f} baseline={baseline:.3f} full={full:.3f}")

    # Segment detection on normalized flux
    is_open = hysteresis_states(norm, thr_open=opts.open_thr, thr_close=opts.close_thr)
    raw_segs = find_segments(is_open, min_len=opts.min_len_frames)
    raw_segs = merge_close_segments(raw_segs, gap_frames=int(opts.merge_gap_s * fps))
    segs = [se for se in raw_segs if is_useful_segment(norm, fps, se[0], se[1], opts)]

    video_duration_s = len(norm) / fps
    pulse_segs = choose_pulse_segments(
        segs, sm, fps,
        commanded_ms=commanded_ms,
        gap_s=gap_s,
    )
    if len(segs) < len(commanded_ms):
        inferred = infer_leading_missing_assignment(
            segs, fps, commanded_ms, gap_s, video_duration_s
        )
        if inferred is not None:
            pulse_segs = inferred

    # Expected pulse schedule for reporting/alignment columns.
    exp_offsets = expected_pulse_starts_s(commanded_ms, gap_s, first_start_s=0.0)
    anchor_start_s = 0.0
    for i, seg_pair in enumerate(pulse_segs):
        if seg_pair is not None:
            anchor_start_s = (seg_pair[0] / fps) - exp_offsets[i]
            break
    exp_starts = exp_offsets + anchor_start_s

    t = np.arange(len(norm)) / fps
    pre_frames = int(opts.pre_window_s * fps)
    post_frames = int(opts.post_window_s * fps)

    base = os.path.splitext(os.path.basename(video_path))[0]
    summary_path = os.path.join(out_dir, f"{base}_summary.csv")
    trace_path = os.path.join(out_dir, f"{base}_aligned_trace.csv")

    # Write aligned trace (per pulse)
    with open(trace_path, "w", newline="") as ftrace:
        tw = csv.writer(ftrace)
        tw.writerow(["video", "pulse_id", "cmd_ms", "t_ms_from_exp_start", "norm_flux"])

        rows = []
        for i, cmd_ms in enumerate(commanded_ms):
            exp_start = exp_starts[i]
            exp_end = exp_start + cmd_ms / 1000.0
            pulse_seg = pulse_segs[i] if i < len(pulse_segs) else None
            segment_found = 1 if pulse_seg is not None else 0

            if pulse_seg is not None:
                s, e = pulse_seg
                i0 = max(0, s - pre_frames)
                i1 = min(len(norm) - 1, e + post_frames)
                eff_i0 = s
                eff_i1 = e
            else:
                win_start = max(0.0, exp_start - opts.pre_window_s)
                win_end = min(t[-1], exp_end + opts.post_window_s)
                i0 = int(win_start * fps)
                i1 = min(len(norm) - 1, int(win_end * fps))
                eff_i0 = i0
                eff_i1 = i1

            if i1 <= i0:
                continue

            seg = norm[i0:i1 + 1]
            seg_t = t[i0:i1 + 1]
            peak_idx_rel = int(np.argmax(seg))
            peak_idx = i0 + peak_idx_rel

            # crossings
            t_rise_02 = crossing_time(t, norm, opts.first_light_thr, "rising", i0, peak_idx)
            t_rise_50 = crossing_time(t, norm, 0.5, "rising", i0, peak_idx)
            t_rise_95 = crossing_time(t, norm, opts.full_thr, "rising", i0, peak_idx)
            t_fall_95 = crossing_time(t, norm, opts.full_thr, "falling", peak_idx, i1)
            t_fall_50 = crossing_time(t, norm, 0.5, "falling", peak_idx, i1)
            t_fall_02 = crossing_time(t, norm, opts.first_light_thr, "falling", peak_idx, i1)

            eff_seg = norm[eff_i0:eff_i1 + 1]
            eff_ms = 1000.0 * float(np.sum(eff_seg)) / fps
            peak_norm = float(np.max(seg))

            sat_max = float(np.max(sat_fracs[eff_i0:eff_i1 + 1])) if sat_fracs.size else 0.0
            at_video_end = i1 >= (len(norm) - 1)
            tail_open = bool(np.mean(norm[max(0, len(norm) - 10):]) > opts.close_thr)
            seg_hits_end = bool(pulse_seg is not None and eff_i1 >= (len(norm) - 2))
            truncated_end = int(seg_hits_end or (at_video_end and (t_fall_02 is None or tail_open)))

            rows.append({
                "video": base,
                "pulse_id": i + 1,
                "cmd_ms": cmd_ms,
                "exp_start_s": f"{exp_start:.6f}",
                "exp_end_s": f"{exp_end:.6f}",
                "segment_start_s": f"{(pulse_seg[0] / fps):.6f}" if pulse_seg is not None else "",
                "segment_end_s": f"{(pulse_seg[1] / fps):.6f}" if pulse_seg is not None else "",
                "eff_ms": f"{eff_ms:.3f}",
                "peak_norm": f"{peak_norm:.3f}",
                "baseline": f"{baseline:.3f}",
                "full": f"{full:.3f}",
                "rise_02_ms": f"{(t_rise_02 - exp_start) * 1000.0:.3f}" if t_rise_02 is not None else "",
                "rise_50_ms": f"{(t_rise_50 - exp_start) * 1000.0:.3f}" if t_rise_50 is not None else "",
                "rise_95_ms": f"{(t_rise_95 - exp_start) * 1000.0:.3f}" if t_rise_95 is not None else "",
                "fall_95_ms": f"{(t_fall_95 - exp_end) * 1000.0:.3f}" if t_fall_95 is not None else "",
                "fall_50_ms": f"{(t_fall_50 - exp_end) * 1000.0:.3f}" if t_fall_50 is not None else "",
                "fall_02_ms": f"{(t_fall_02 - exp_end) * 1000.0:.3f}" if t_fall_02 is not None else "",
                "sat_frac_max": f"{sat_max:.3f}",
                "segment_found": segment_found,
                "is_truncated": truncated_end,
            })

            for j, v in enumerate(seg):
                t_rel_ms = (seg_t[j] - exp_start) * 1000.0
                tw.writerow([base, i + 1, cmd_ms, f"{t_rel_ms:.3f}", f"{v:.6f}"])

    with open(summary_path, "w", newline="") as fsum:
        if rows:
            dw = csv.DictWriter(fsum, fieldnames=list(rows[0].keys()))
            dw.writeheader()
            dw.writerows(rows)

    return rows


def main():
    parser = argparse.ArgumentParser(description="Analyze shutter flux vs time from AVI videos.")
    parser.add_argument("videos", nargs="+", help="Input AVI videos")
    parser.add_argument("--durations", choices=sorted(DURATIONS.keys()), default="durations1")
    parser.add_argument("--durations-list", help="Comma-separated custom list of ms durations")
    parser.add_argument("--gap-s", type=float, default=3.0)
    parser.add_argument("--out-dir", default="results_flux_new")
    parser.add_argument("--mode", choices=["APERTURE", "ANNULUS", "RING", "TOPKCAP"], default="RING")
    parser.add_argument("--normalization", choices=["per-video", "reference"], default="per-video")
    parser.add_argument("--smooth", type=int, default=5)
    parser.add_argument("--open-thr", type=float, default=0.05)
    parser.add_argument("--close-thr", type=float, default=0.02)
    parser.add_argument("--merge-gap-s", type=float, default=0.05)
    parser.add_argument("--min-len-frames", type=int, default=2)
    parser.add_argument("--pre-window-s", type=float, default=0.2)
    parser.add_argument("--post-window-s", type=float, default=0.2)
    parser.add_argument("--first-light-thr", type=float, default=0.02)
    parser.add_argument("--full-thr", type=float, default=0.95)
    parser.add_argument("--r-ap", type=int, default=8)
    parser.add_argument("--r-bg-in", type=int, default=75)
    parser.add_argument("--r-bg-out", type=int, default=105)
    parser.add_argument("--r-in", type=int, default=25)
    parser.add_argument("--r-out", type=int, default=60)
    parser.add_argument("--sat-thr", type=int, default=250)
    parser.add_argument("--topk", type=int, default=250)
    parser.add_argument("--sat-cap", type=int, default=240)
    parser.add_argument("--useful-eff-ms", type=float, default=5.0)
    parser.add_argument("--useful-peak-norm", type=float, default=0.20)
    parser.add_argument("--quick-topk", type=int, default=80)
    parser.add_argument("--center-topk", type=int, default=100)
    parser.add_argument("--use-ffmpeg", action="store_true", help="Decode frames with ffmpeg (useful for pal8 AVI).")
    parser.add_argument("--center-max-frames", type=int, default=2000)
    parser.add_argument("--ref-video", default=None, help="Video used to determine fixed star center")
    parser.add_argument("--baseline-video", default=None, help="Long closed video for baseline reference")
    parser.add_argument("--full-video", default=None, help="Long open video for full reference")
    parser.add_argument("--baseline-percentile", type=float, default=5.0)
    parser.add_argument("--full-percentile", type=float, default=95.0)
    parser.add_argument("--keep-truncated", action="store_true", help="Include truncated pulses in calibration output.")
    parser.add_argument("--keep-missing", action="store_true", help="Include pulses with no matched segment in calibration output.")

    args = parser.parse_args()
    args.mode = args.mode.upper()

    commanded_ms = parse_durations(args)
    os.makedirs(args.out_dir, exist_ok=True)

    # Determine a fixed center + masks
    ref_source = args.ref_video or args.full_video or args.videos[0]
    (cx, cy), (h, w) = center_from_video(
        ref_source, args, use_ffmpeg=args.use_ffmpeg, max_frames=args.center_max_frames
    )
    masks = build_masks(h, w, cx, cy, args)

    baseline_ref = None
    full_ref = None
    if args.normalization == "reference":
        if args.baseline_video:
            baseline_ref = compute_reference_level(
                args.baseline_video, args, (cx, cy), masks, which="baseline", use_ffmpeg=args.use_ffmpeg
            )
            print(f"Baseline reference from {args.baseline_video}: {baseline_ref:.3f}")
        if args.full_video:
            full_ref = compute_reference_level(
                args.full_video, args, (cx, cy), masks, which="full", use_ffmpeg=args.use_ffmpeg
            )
            print(f"Full reference from {args.full_video}: {full_ref:.3f}")
    elif args.baseline_video or args.full_video:
        print("[NOTE] Ignoring --baseline-video/--full-video because --normalization=per-video.")

    all_rows = []
    for vp in args.videos:
        rows = analyze_video(
            vp, commanded_ms, args.gap_s, args.out_dir, args,
            center=(cx, cy), masks=masks,
            baseline_ref=baseline_ref, full_ref=full_ref,
            use_ffmpeg=args.use_ffmpeg,
        )
        all_rows.extend(rows)

    # Aggregate per commanded duration
    agg = {}
    for r in all_rows:
        if (not args.keep_missing) and int(r.get("segment_found", 1)) == 0:
            continue
        if (not args.keep_truncated) and int(r.get("is_truncated", 0)) == 1:
            continue
        cmd = int(r["cmd_ms"])
        eff = float(r["eff_ms"])
        agg.setdefault(cmd, []).append(eff)

    calib_path = os.path.join(args.out_dir, "calibration_table.csv")
    with open(calib_path, "w", newline="") as fcal:
        w = csv.writer(fcal)
        w.writerow(["cmd_ms", "n", "eff_ms_mean", "eff_ms_median", "eff_ms_std"])
        for cmd in sorted(agg.keys()):
            vals = np.asarray(agg[cmd], dtype=float)
            w.writerow([cmd, len(vals), f"{np.mean(vals):.3f}", f"{np.median(vals):.3f}", f"{np.std(vals):.3f}"])

    print(f"Wrote calibration: {calib_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyze_shutter_flux.py <video1.avi> [video2.avi ...]")
        sys.exit(1)
    main()
