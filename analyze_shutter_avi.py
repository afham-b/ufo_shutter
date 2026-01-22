import csv
import sys
import math
import cv2
import numpy as np

def moving_average(x, w):
    if w <= 1:
        return x
    x = np.asarray(x, dtype=float)
    kernel = np.ones(w) / w
    return np.convolve(x, kernel, mode="same")

def find_segments(is_open, min_len=3):
    """
    Return list of (start_idx, end_idx) for contiguous True regions.
    end_idx is inclusive.
    min_len is in frames.
    """
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

def robust_threshold(signal):
    """
    Compute a threshold between dark and bright using percentiles.
    """
    s = np.asarray(signal, dtype=float)
    lo = np.percentile(s, 10)
    hi = np.percentile(s, 90)
    # threshold halfway between low/high clusters
    return (lo + hi) / 2.0

def main(video_path, output_csv="shutter_timing_results.csv"):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or math.isnan(fps) or fps <= 1:
        # fallback if fps metadata is missing; user said ~110
        fps = 110.0
        print("[WARN] FPS metadata missing. Using 110 fps fallback.")

    means = []
    frame_count = 0

    # If you want to force an ROI, uncomment these and set values:
    # ROI = (x, y, w, h)
    ROI = None

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_count += 1

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if ROI is not None:
            x, y, w, h = ROI
            gray = gray[y:y+h, x:x+w]

        means.append(float(np.mean(gray)))

    cap.release()

    means = np.asarray(means, dtype=float)
    if len(means) < 10:
        raise RuntimeError("Video too short or failed to read frames.")

    # Smooth a little to reduce sensor noise; window ~3 frames (~27 ms at 110 fps)
    #testing at 1 frame for sharper transitions
    sm = moving_average(means, 1)

    thr = robust_threshold(sm)
    is_open = sm > thr

    # Find open segments; require at least 2 frames open (~18 ms)
    #testing at 2 frame min len
    segs = find_segments(is_open, min_len=2)

    print(f"Frames: {len(means)}  FPS: {fps:.2f}  Threshold: {thr:.2f}")
    print(f"Detected open segments: {len(segs)}")

    # Output results
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
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            w.writeheader()
            w.writerows(rows)

    print(f"Wrote: {output_csv}")

    # Quick estimate of a single constant "loss" if you ran known commanded durations:
    # If your sweep list is known, you can paste it here to compare.
    #commanded = [10,20,30,50,75,100,150,200,250,300,500,750,1000,1500,2000,2500,3000]
    #testing 
    commanded = [10,20,30,50,75,100,150,200,250,260,270,280,287,290,300,310,500,750,1000,1500,2000,2500,3000,4000]
    if len(rows) == len(commanded):
        measured = np.array([float(r["open_ms_measured"]) for r in rows])
        cmd = np.array(commanded, dtype=float)
        # Fit measured ≈ cmd - loss  => measured = 1*cmd + b, loss = -b
        A = np.vstack([cmd, np.ones_like(cmd)]).T
        m, b = np.linalg.lstsq(A, measured, rcond=None)[0]
        loss = -b
        print(f"\nFit measured_ms ≈ {m:.4f}*cmd_ms + {b:.2f}")
        print(f"Estimated constant loss (ms): {loss:.2f}")
        print("Suggested correction: cmd_ms = target_ms + loss_ms")
    else:
        print("\n[NOTE] Number of detected segments != number of expected sweep pulses.")
        print("If detection missed/merged pulses, increase gap_s in sweep or adjust min_len/thresholding.")
        print("You can still use the CSV to manually pair commanded vs measured.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyze_shutter_avi.py /path/to/video.avi")
        sys.exit(1)
    main(sys.argv[1])


#>install ffmpeg if not present
#use ffmpeg to convert avi to mpeg if needed 
#example ffmpeg command to go to mjpeg codec with quality 1 (lower is better, 1 is best): 
# ffmpeg -i "/Users/afhambashir/ASICAP/CapObj/2026-01-21Z/2026-01-21-2104_9-CapObj.AVI" \
#   -an -c:v mjpeg -q:v 1 \
#   "/Users/afhambashir/ASICAP/CapObj/2026-01-21Z/2026-01-21-2104_9-CapObj_mjpeg.avi"


#python3 analyze_shutter_avi.py /Users/afhambashir/ASICAP/2026-01-21-2104_9-CapObj.AVI
