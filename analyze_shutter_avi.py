import csv
import sys
import math
import cv2
import numpy as np


# ---------- Helpers ----------
def moving_average(x, w):
    if w <= 1:
        return x
    x = np.asarray(x, dtype=float)
    kernel = np.ones(w) / w
    return np.convolve(x, kernel, mode="same")


def find_segments(is_open, min_len=3):
    """Return list of (start_idx, end_idx) for contiguous True regions (inclusive)."""
    segs = []
    start = None
    for i, v in enumerate(is_open):
        if v and start is None:
            start = i
        if (not v) and start is not None:
            end = i - 1
            if end - start + 1 >= min_len:
                segs.append((start, end))
            start = None
    if start is not None:
        end = len(is_open) - 1
        if end - start + 1 >= min_len:
            segs.append((start, end))
    return segs


def robust_levels(signal, lo_p=10, hi_p=90):
    s = np.asarray(signal, dtype=float)
    lo = float(np.percentile(s, lo_p))
    hi = float(np.percentile(s, hi_p))
    mid = (lo + hi) / 2.0
    return mid, lo, hi

def hysteresis_states(signal, thr_open, thr_close):
    """
    Simple hysteresis:
      - if state is closed, only open when signal >= thr_open
      - if state is open, only close when signal <= thr_close
    """
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

def merge_close_segments(segs, gap_frames):
    """
    Merge segments if the gap between them is <= gap_frames.
    segs is list of (start, end) inclusive.
    """
    if not segs:
        return []
    segs = sorted(segs, key=lambda x: x[0])
    out = [list(segs[0])]
    for s, e in segs[1:]:
        ps, pe = out[-1]
        if s - pe - 1 <= gap_frames:
            out[-1][1] = max(pe, e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


def pick_one_segment_per_pulse(segs, fps, gap_s=2.0):
    """
    Groups segments that belong to the same commanded pulse window
    and keeps the longest segment per window.
    """
    if not segs:
        return []
    segs = sorted(segs, key=lambda x: x[0])

    # half the commanded gap: anything within this is "same pulse window"
    max_intragap = int(0.5 * gap_s * fps)

    groups = []
    cur = [segs[0]]
    anchor_start = segs[0][0]

    for s, e in segs[1:]:
        if s - anchor_start <= max_intragap:
            cur.append((s, e))
        else:
            groups.append(cur)
            cur = [(s, e)]
            anchor_start = s
    groups.append(cur)

    # keep longest in each group
    out = []
    for g in groups:
        out.append(max(g, key=lambda se: se[1] - se[0]))
    return out


def frame_to_gray(frame):
    # OpenCV sometimes returns already-grayscale frames depending on codec.
    if frame is None:
        return None
    if len(frame.shape) == 2:
        return frame
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def star_metric(gray, topk=80):
    """
    Robust for a star-like point source anywhere in the frame:
    metric = mean(top K brightest pixels) - median(pixel values)
    """
    g = gray.astype(np.float32, copy=False)
    flat = g.reshape(-1)

    # Background estimate
    med = float(np.median(flat))

    # TopK brightest pixels anywhere
    k = int(max(1, min(topk, flat.size)))
    top = np.partition(flat, -k)[-k:]
    return float(np.mean(top) - med)


# ---------- Main ----------
def main(video_path, output_csv="shutter_timing_results.csv"):
    # Tunables
    TOPK = 200               # try 50–200 depending on blob size (you said ~20px blob -> 80 is fine)
    SMOOTH_W = 5            # 3–9; larger reduces fragmentation
    MIN_LEN_FRAMES = 4      # ignore tiny chatter
    HYST_FRAC = 0.03        # fraction of swing for hysteresis band
    MODE = "full"           # "any" or "full"
    FULL_FRAC = 0.97        # for MODE="full": require near-max brightness

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    MERGE_GAP_FRAMES = int(0.30 * fps)   # ~150 frames at 508 fps

    if not fps or math.isnan(fps) or fps <= 1:
        fps = 110.0
        print("[WARN] FPS metadata missing. Using 110 fps fallback.")

    metrics = []
    frame_count = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_count += 1

        gray = frame_to_gray(frame)
        if gray is None or gray.size == 0:
            continue

        m = star_metric(gray, topk=TOPK)
        metrics.append(m)

    cap.release()

    metrics = np.asarray(metrics, dtype=float)
    if len(metrics) < 10:
        raise RuntimeError("Video too short or failed to read frames.")

    sm = moving_average(metrics, SMOOTH_W)

    thr_mid, lo, hi = robust_levels(sm, lo_p=10, hi_p=90)
    swing = max(0.0, hi - lo)

    print(f"Frames: {len(metrics)}  FPS: {fps:.2f}")
    print(f"Metric levels: closed~{lo:.3f}, open~{hi:.3f}, swing~{swing:.3f}")

    # If swing is tiny, your metric isn't distinguishing states -> stop early with advice
    if swing < 1e-3:
        print("[ERROR] swing≈0: metric not distinguishing open/closed.")
        print("This usually means the blob signal isn't being captured (or video is too uniform).")
        print("Try increasing TOPK (e.g. 200) or verify the star blob is present in frames.")
        # Still write empty CSV for consistency
        with open(output_csv, "w", newline="") as f:
            pass
        return

    # Hysteresis thresholds for MODE="any"
    band = HYST_FRAC * swing
    thr_open = thr_mid + band
    thr_close = thr_mid - band
    is_open_any = hysteresis_states(sm, thr_open=thr_open, thr_close=thr_close)

    # Full-open threshold near top plateau for MODE="full"
    # Full-open thresholds (hysteresis band)
    full_thr_open  = lo + FULL_FRAC * swing
    # allow a small dip without "closing" the open state
    FULL_HYST_FRAC = 0.03   # try 0.02–0.06
    full_thr_close = full_thr_open - FULL_HYST_FRAC * swing

    is_open_full = hysteresis_states(sm, thr_open=full_thr_open, thr_close=full_thr_close)

    is_open = is_open_full if MODE == "full" else is_open_any

    print(f"Full-open: FULL_FRAC={FULL_FRAC:.2f} open_thr={full_thr_open:.3f} close_thr={full_thr_close:.3f}  MODE={MODE}")

    # 1) Find raw segments first
    segs = find_segments(is_open, min_len=MIN_LEN_FRAMES)
    print(f"Detected open segments (raw): {len(segs)}")

    # 2) Merge short gaps that are really the same pulse (closing bounce, spikes, etc.)
    MERGE_GAP_FRAMES = int(0.30 * fps)   # 0.30s is safe if your sweep gap is ~2s+
    segs = merge_close_segments(segs, gap_frames=MERGE_GAP_FRAMES)
    print(f"Segments after merge: {len(segs)}")

    # 3) OPTIONAL: force 1 segment per commanded pulse window (recommended)
    GAP_S = 2.0  # set this to your actual sweep gap (2.0 or 3.0)
    segs = pick_one_segment_per_pulse(segs, fps, gap_s=GAP_S)
    print(f"Final segments (1 per pulse window): {len(segs)}")

    crossings = np.sum((sm[:-1] >= full_thr_open) != (sm[1:] >= full_thr_open))
    print("Raw crossings vs full_thr_open:", crossings)

    # Output CSV
    rows = []
    for idx, (s, e) in enumerate(segs, start=1):
        open_frames = e - s + 1
        open_ms = 1000.0 * open_frames / fps
        start_s = s / fps
        end_s = e / fps
        rows.append({
            "pulse_index": idx,
            "start_frame": s,
            "end_frame": e,
            "start_time_s": f"{start_s:.6f}",
            "end_time_s": f"{end_s:.6f}",
            "open_frames": open_frames,
            "open_ms_measured": f"{open_ms:.3f}",
        })

    with open(output_csv, "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print(f"Wrote: {output_csv}")

    # Optional regression if counts match commanded list
    durations1 = [10,20,30,50,75,100,150,200,250,260,270,280,287,290,300,310,500,750,1000,1500,2000,2500,3000,4000]
    durations2 = [10,12,15,17,20,22,24,26,28,30,32,34,36,38,40,42,45,50,60,75,80,85,100,150]
    durations3 = [10,11,12,13,14,15,16,17,18,19,20,22,24,26,28,30]
    durations4 = [75,76,77,78,79,80,81,82,83,84,85]

    commanded = durations1
    
    if len(rows) == len(commanded):
        measured = np.array([float(r["open_ms_measured"]) for r in rows])
        cmd = np.array(commanded, dtype=float)
        A = np.vstack([cmd, np.ones_like(cmd)]).T
        m, b = np.linalg.lstsq(A, measured, rcond=None)[0]
        print(f"\nFit measured_ms ≈ {m:.4f}*cmd_ms + {b:.2f}")
    else:
        print("\n[NOTE] Number of detected segments != number of expected sweep pulses.")
        print("If detection missed/merged pulses:")
        print(" - Increase sweep gap_s (separate pulses more)")
        print(" - Increase MIN_LEN_FRAMES (e.g., 4–6) to ignore chatter")
        print(" - Increase SMOOTH_W or FULL_FRAC to reduce fragmentation")
        print(" - Adjust TOPK for blob size/brightness")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyze_shutter_avi.py /path/to/video.avi")
        sys.exit(1)
    main(sys.argv[1])

#for example run:
#python3 analyze_shutter_avi.py /Users/afhambashir/ASICAP/CapObj/50us/2026-02-04-2232_4-CapObj.AVI


#50us notes
#@508fps, at 50us exposure, and 50 gain settings
#gap = 2 seconds 
#5:33-5:38 pm recordings are durations 1= [10,20,30,50,75,100,150,200,250,260,270,280,287,290,300,310,500,750,1000,1500,2000,2500,3000,4000]

#gap = 3 seconds
#5:46-5:52 pm recordings are durations 2= [10,12,15,17,20,22,24,26,28,30,32,34,36,38,40,42,45,50,60,75,80,85,100,150]

# tests for first lights 
#5:58-5:59 pm recordings are durations 3= [10,11,12,13,14,15,16,17,18,19,20,22,24,26,28,30]

#test for full exposure time. 
#6:01-6:02 pm recordings are durations 4= [75,76,77,78,79,80,81,82,83,84,85]
