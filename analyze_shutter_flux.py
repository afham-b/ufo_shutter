import csv
import sys
import math
import cv2
import numpy as np


# ----------------- Helpers -----------------
def moving_average(x, w):
    if w <= 1:
        return x
    x = np.asarray(x, dtype=float)
    kernel = np.ones(w, dtype=float) / float(w)
    return np.convolve(x, kernel, mode="same")


def frame_to_gray(frame):
    if frame is None:
        return None
    if len(frame.shape) == 2:
        return frame
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def star_metric(gray, topk=200):
    """
    For star-like point source anywhere:
    metric = mean(top K brightest pixels) - median(background)
    """
    g = gray.astype(np.float32, copy=False)
    flat = g.reshape(-1)
    med = float(np.median(flat))
    k = int(max(1, min(int(topk), flat.size)))
    top = np.partition(flat, -k)[-k:]
    return float(np.mean(top) - med)


def robust_levels(signal, lo_p=10, hi_p=90):
    s = np.asarray(signal, dtype=float)
    lo = float(np.percentile(s, lo_p))
    hi = float(np.percentile(s, hi_p))
    mid = 0.5 * (lo + hi)
    return mid, lo, hi


def hysteresis_states(signal, thr_open, thr_close):
    """
    closed -> open when >= thr_open
    open   -> close when <= thr_close
    """
    s = np.asarray(signal, dtype=float)
    out = np.zeros(len(s), dtype=bool)
    state = False
    for i, v in enumerate(s):
        if (not state) and v >= thr_open:
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
    """Merge segments if they are separated by <= gap_frames."""
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


def segment_stats(sm, s, e):
    """Compute peak above baseline and AUC above baseline for a segment."""
    seg = sm[s:e+1]
    baseline = float(np.percentile(sm, 10))   # same idea as your 'closed~lo'
    peak = float(np.max(seg))
    peak_bs = peak - baseline
    auc_bs = float(np.sum(np.maximum(seg - baseline, 0.0)))  # in "metric*frames"
    return peak_bs, auc_bs, baseline


def filter_segments_by_strength(segs, sm, fps, peak_min_bs=10.0, auc_min_ms=5.0):
    """
    Remove tiny chatter segments.
    - peak_min_bs: minimum peak above baseline (kills your peak_bs=1 junk)
    - auc_min_ms: minimum "area" roughly scaled into ms-equivalent
    """
    kept = []
    for s, e in segs:
        peak_bs, auc_bs_frames, baseline = segment_stats(sm, s, e)

        # Convert auc from (metric*frames) -> (metric*seconds)
        auc_bs_s = auc_bs_frames / fps

        # Very rough AUC floor: require at least auc_min_ms worth of “bright”
        # (This is optional, peak_min_bs is usually enough in your case.)
        if peak_bs >= peak_min_bs and auc_bs_s > (auc_min_ms / 1000.0):
            kept.append((s, e))
    return kept


def pick_one_segment_per_pulse_window(segs, sm, fps, boundary_gap_s=1.0):
    """
    If multiple segments still occur within a single pulse window,
    cluster by time gaps and keep the strongest (max AUC) segment per cluster.
    """
    if not segs:
        return []

    segs = sorted(segs)
    starts = np.array([s for s, e in segs], dtype=float) / fps
    gaps = np.diff(starts)

    clusters = []
    start_i = 0
    for i, g in enumerate(gaps):
        if g > boundary_gap_s:
            clusters.append((start_i, i))
            start_i = i + 1
    clusters.append((start_i, len(segs) - 1))

    best = []
    for a, b in clusters:
        group = segs[a:b+1]

        # choose by max AUC above baseline
        best_seg = None
        best_auc = -1.0
        for s, e in group:
            _, auc_bs_frames, _ = segment_stats(sm, s, e)
            auc_bs_s = auc_bs_frames / fps
            if auc_bs_s > best_auc:
                best_auc = auc_bs_s
                best_seg = (s, e)

        best.append(best_seg)

    return best


def first_crossing_time(frames, values, thr):
    """
    Return first frame index in this pulse where values >= thr.
    frames: np.array of absolute frame indices
    values: np.array metric values aligned with frames
    """
    idx = np.where(values >= thr)[0]
    if idx.size == 0:
        return None
    return int(frames[idx[0]])


def last_crossing_time(frames, values, thr):
    idx = np.where(values >= thr)[0]
    if idx.size == 0:
        return None
    return int(frames[idx[-1]])


# ----------------- Main -----------------
def main(video_path,
         out_summary_csv="pulse_flux_summary.csv",
         out_trace_csv="pulse_flux_trace.csv"):

    # --- Tunables ---
    TOPK = 200            # blob ~20 px: 80–250 are reasonable; 200 is safe
    SMOOTH_W = 5          # 3–9, reduce chatter
    MIN_LEN_FRAMES = 3    # ignore tiny flicker segments
    HYST_FRAC = 0.10      # hysteresis band fraction of swing
    MERGE_GAP_FRAMES = 12 # merge brief dropouts (~ at 508fps: 12 frames ~ 24ms)

    # Pulse-local threshold fractions (relative to that pulse's own peak)
    FRACS = [0.10, 0.50, 0.90]  # time above 10%, 50%, 90% of peak (after baseline subtraction)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or math.isnan(fps) or fps <= 1:
        fps = 110.0
        print("[WARN] FPS metadata missing. Using 110 fps fallback.")

    metrics = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = frame_to_gray(frame)
        if gray is None or gray.size == 0:
            metrics.append(0.0)
            continue
        metrics.append(star_metric(gray, topk=TOPK))
    cap.release()

    metrics = np.asarray(metrics, dtype=float)
    if metrics.size < 10:
        raise RuntimeError("Video too short or failed to read frames.")

    sm = moving_average(metrics, SMOOTH_W)
    thr_mid, lo, hi = robust_levels(sm, lo_p=10, hi_p=90)
    swing = max(0.0, hi - lo)

    print(f"Frames: {len(metrics)}  FPS: {fps:.2f}")
    print(f"Metric levels: closed~{lo:.3f}, open~{hi:.3f}, swing~{swing:.3f}")

    if swing < 1e-3:
        print("[ERROR] swing≈0: metric not distinguishing open/closed.")
        print("Likely frames are black/blank or blob not captured.")
        # write empty outputs
        open(out_summary_csv, "w").close()
        open(out_trace_csv, "w").close()
        return

    band = HYST_FRAC * swing
    thr_open = thr_mid + band
    thr_close = thr_mid - band
    is_open_any = hysteresis_states(sm, thr_open=thr_open, thr_close=thr_close)

    segs = find_segments(is_open_any, min_len=MIN_LEN_FRAMES)

    # Merge small gaps (tune MERGE_GAP_FRAMES to your camera + threshold behavior)
    MERGE_GAP_FRAMES = int(0.06 * fps)   # ~60 ms; good starting point at 508 fps (~30 frames)
    segs = merge_close_segments(segs, gap_frames=MERGE_GAP_FRAMES)

    # Kill chatter: this is the key fix for your "41 instead of 24" problem
    # In your run, junk segments have peak_bs=1.0; real ones are ~255.
    segs = filter_segments_by_strength(
        segs, sm, fps,
        peak_min_bs=10.0,     # keep anything with real brightness
        auc_min_ms=5.0        # optional; helps remove single-frame junk
    )

    # If you still occasionally get more than expected, cluster within pulse windows:
    # Estimate a boundary gap from your sweep gap (if you used ~2s, use 1.0–1.5s here).
    segs = pick_one_segment_per_pulse_window(segs, sm, fps, boundary_gap_s=2.0)

    print(f"Final pulse segments: {len(segs)}")


    print(f"Hysteresis(any): thr_open={thr_open:.3f}, thr_close={thr_close:.3f}")
    print(f"Detected pulse segments (after merge): {len(segs)}")

    # ---- Write per-frame trace CSV ----
    # Contains pulse_id, abs_frame, time_s, metric_raw, metric_sm
    with open(out_trace_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pulse_id", "abs_frame", "time_s", "metric_raw", "metric_sm"])

        for pid, (s, e) in enumerate(segs, start=1):
            for fr in range(s, e + 1):
                w.writerow([
                    pid,
                    fr,
                    f"{fr / fps:.6f}",
                    f"{metrics[fr]:.6f}",
                    f"{sm[fr]:.6f}",
                ])

    # ---- Summary per pulse ----
    summary_rows = []
    for pid, (s, e) in enumerate(segs, start=1):
        frames = np.arange(s, e + 1, dtype=int)
        vals = sm[s:e + 1]

        # Baseline for THIS pulse: take a small window before pulse if available, else global lo
        pre_n = int(min(50, s))  # up to 50 frames pre
        if pre_n >= 5:
            baseline = float(np.median(sm[s - pre_n:s]))
        else:
            baseline = float(lo)

        vals_bs = vals - baseline  # baseline-subtracted
        vals_bs[vals_bs < 0] = 0.0

        peak = float(np.max(vals_bs))
        peak_idx = int(np.argmax(vals_bs))
        peak_frame = int(frames[peak_idx])

        dur_frames = int(e - s + 1)
        dur_ms = 1000.0 * dur_frames / fps

        # Integrated flux (area above baseline) in "metric*seconds"
        # dt = 1/fps
        auc = float(np.sum(vals_bs) / fps)

        # Fractions-of-peak timings
        frac_results = {}
        if peak > 1e-9:
            for frac in FRACS:
                thr = frac * peak
                f_start = first_crossing_time(frames, vals_bs, thr)
                f_end = last_crossing_time(frames, vals_bs, thr)
                if f_start is None or f_end is None or f_end < f_start:
                    frac_results[frac] = (None, None, 0.0)
                else:
                    t_ms = 1000.0 * (f_end - f_start + 1) / fps
                    frac_results[frac] = (f_start, f_end, t_ms)
        else:
            for frac in FRACS:
                frac_results[frac] = (None, None, 0.0)

        row = {
            "pulse_id": pid,
            "start_frame": s,
            "end_frame": e,
            "start_time_s": f"{s / fps:.6f}",
            "end_time_s": f"{e / fps:.6f}",
            "segment_ms_any": f"{dur_ms:.3f}",

            "baseline": f"{baseline:.3f}",
            "peak_bs": f"{peak:.3f}",              # peak above baseline
            "peak_frame": peak_frame,
            "peak_time_s": f"{peak_frame / fps:.6f}",
            "auc_bs_metric_s": f"{auc:.6f}",
        }

        # Add frac timing columns
        for frac in FRACS:
            f_start, f_end, t_ms = frac_results[frac]
            tag = int(frac * 100)
            row[f"t{tag}_start_frame"] = "" if f_start is None else f_start
            row[f"t{tag}_end_frame"] = "" if f_end is None else f_end
            row[f"t{tag}_ms"] = f"{t_ms:.3f}"

        summary_rows.append(row)

    # Write summary CSV
    fieldnames = list(summary_rows[0].keys()) if summary_rows else []
    with open(out_summary_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if fieldnames:
            w.writeheader()
            w.writerows(summary_rows)

    print(f"Wrote: {out_summary_csv}")
    print(f"Wrote: {out_trace_csv}")
    print("\nInterpretation tips:")
    print("- peak_bs tells you max throughput achieved in that pulse (above baseline).")
    print("- t90_ms is time spent above 90% of that pulse’s peak (not global max).")
    print("- auc_bs_metric_s is integrated light (useful for exposure equivalence).")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyze_shutter_flux.py /path/to/video.avi")
        sys.exit(1)
    main(sys.argv[1])
