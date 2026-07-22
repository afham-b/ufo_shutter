import argparse
import csv
import glob
import math
import os

import numpy as np

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    plt = None


def to_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def load_summary_rows(results_dirs):
    rows = []
    for results_dir in results_dirs:
        for path in sorted(glob.glob(os.path.join(results_dir, "*_summary.csv"))):
            with open(path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    item = {
                        "results_dir": results_dir,
                        "summary_path": path,
                        "video": row["video"],
                        "pulse_id": to_int(row["pulse_id"]),
                        "cmd_ms": to_int(row["cmd_ms"]),
                        "eff_ms": to_float(row["eff_ms"]),
                        "peak_norm": to_float(row["peak_norm"]),
                        "segment_found": to_int(row.get("segment_found", 1)),
                        "is_truncated": to_int(row.get("is_truncated", 0)),
                    }
                    rows.append(item)
    return rows


def load_trace_samples(results_dirs, summary_map):
    samples = []
    for results_dir in results_dirs:
        for path in sorted(glob.glob(os.path.join(results_dir, "*_aligned_trace.csv"))):
            with open(path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = (row["video"], to_int(row["pulse_id"]))
                    meta = summary_map.get(key)
                    if meta is None:
                        continue
                    samples.append({
                        "video": row["video"],
                        "pulse_id": to_int(row["pulse_id"]),
                        "cmd_ms": to_int(row["cmd_ms"]),
                        "t_ms": to_float(row["t_ms_from_exp_start"]),
                        "flux": to_float(row["norm_flux"]),
                        "segment_found": meta["segment_found"],
                        "is_truncated": meta["is_truncated"],
                        "peak_norm": meta["peak_norm"],
                    })
    return samples


def group_trace_samples(trace_samples):
    grouped = {}
    for row in trace_samples:
        key = (row["video"], row["pulse_id"])
        grouped.setdefault(key, []).append((row["t_ms"], row["flux"], row["cmd_ms"]))

    out = []
    for key, pts in grouped.items():
        pts = sorted(pts, key=lambda p: p[0])
        t = np.asarray([p[0] for p in pts], dtype=float)
        y = np.asarray([p[1] for p in pts], dtype=float)
        cmd_ms = int(pts[0][2])
        out.append({
            "video": key[0],
            "pulse_id": key[1],
            "cmd_ms": cmd_ms,
            "t_ms": t,
            "flux": y,
        })
    return out


def aggregate_command_effective(summary_rows):
    by_cmd = {}
    for row in summary_rows:
        if row["segment_found"] != 1 or row["is_truncated"] == 1:
            continue
        by_cmd.setdefault(row["cmd_ms"], []).append(row["eff_ms"])

    out = []
    for cmd_ms in sorted(by_cmd):
        vals = np.asarray(by_cmd[cmd_ms], dtype=float)
        out.append({
            "cmd_ms": cmd_ms,
            "n": len(vals),
            "eff_ms_mean": float(np.mean(vals)),
            "eff_ms_median": float(np.median(vals)),
            "eff_ms_std": float(np.std(vals)),
        })
    return out


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
                    return float(t[i])
                frac = (thr - y0) / (y1 - y0)
                return float(t[i - 1] + frac * (t[i] - t[i - 1]))
    else:
        for i in range(start_idx + 1, end_idx + 1):
            if y[i - 1] > thr >= y[i]:
                y0, y1 = y[i - 1], y[i]
                if y1 == y0:
                    return float(t[i])
                frac = (y0 - thr) / (y0 - y1)
                return float(t[i - 1] + frac * (t[i] - t[i - 1]))
    return None


def build_opening_curve(pulse_traces, summary_map, onset_threshold, pre_ms, post_ms):
    points = []
    metrics = []
    for pulse in pulse_traces:
        key = (pulse["video"], pulse["pulse_id"])
        meta = summary_map.get(key)
        if meta is None or meta["segment_found"] != 1 or meta["is_truncated"] == 1:
            continue

        t = pulse["t_ms"]
        y = pulse["flux"]
        if len(y) < 3:
            continue

        peak_idx = int(np.argmax(y))
        t_onset = crossing_time(t, y, onset_threshold, "rising", 0, peak_idx)
        t_mid = crossing_time(t, y, 0.50, "rising", 0, peak_idx)
        t_full = crossing_time(t, y, 0.95, "rising", 0, peak_idx)
        if t_onset is None:
            continue

        if t_mid is not None:
            metrics.append({"metric": "opening_from_0.15_to_0.50_ms", "value_ms": t_mid - t_onset})
        if t_full is not None:
            metrics.append({"metric": "opening_from_0.15_to_0.95_ms", "value_ms": t_full - t_onset})

        shifted = t - t_onset
        mask = (shifted >= -pre_ms) & (shifted <= post_ms)
        for tt, yy in zip(shifted[mask], y[mask]):
            points.append((float(tt), float(yy)))
    return points, metrics


def build_retraction_curve(pulse_traces, summary_map, closing_min_cmd_ms, closing_peak_min,
                           pre_ms, post_ms, onset_threshold):
    points = []
    metrics = []
    for pulse in pulse_traces:
        key = (pulse["video"], pulse["pulse_id"])
        meta = summary_map.get(key)
        if meta is None or meta["segment_found"] != 1 or meta["is_truncated"] == 1:
            continue
        if meta["cmd_ms"] < closing_min_cmd_ms or meta["peak_norm"] < closing_peak_min:
            continue

        t = pulse["t_ms"]
        y = pulse["flux"]
        if len(y) < 3:
            continue

        peak_idx = int(np.argmax(y))
        t_fall95 = crossing_time(t, y, 0.95, "falling", peak_idx, len(y) - 1)
        if t_fall95 is None:
            continue

        t_fall50 = crossing_time(t, y, 0.50, "falling", peak_idx, len(y) - 1)
        t_fall15 = crossing_time(t, y, onset_threshold, "falling", peak_idx, len(y) - 1)
        t_fall02 = crossing_time(t, y, 0.02, "falling", peak_idx, len(y) - 1)

        if t_fall50 is not None:
            metrics.append({"metric": "retraction_from_0.95_to_0.50_ms", "value_ms": t_fall50 - t_fall95})
        if t_fall15 is not None:
            metrics.append({"metric": "retraction_from_0.95_to_0.15_ms", "value_ms": t_fall15 - t_fall95})
        if t_fall02 is not None:
            metrics.append({"metric": "retraction_from_0.95_to_0.02_ms", "value_ms": t_fall02 - t_fall95})

        shifted = t - t_fall95
        mask = (shifted >= -pre_ms) & (shifted <= post_ms)
        for tt, yy in zip(shifted[mask], y[mask]):
            points.append((float(tt), float(yy)))
    return points, metrics


def bin_points(points, bin_ms=1.0):
    if not points:
        return []

    t_vals = np.asarray([p[0] for p in points], dtype=float)
    y_vals = np.asarray([p[1] for p in points], dtype=float)

    t_min = float(math.floor(np.min(t_vals) / bin_ms) * bin_ms)
    t_max = float(math.ceil(np.max(t_vals) / bin_ms) * bin_ms)
    edges = np.arange(t_min, t_max + bin_ms, bin_ms)
    if len(edges) < 2:
        return []

    idx = np.floor((t_vals - t_min) / bin_ms).astype(int)
    idx = np.clip(idx, 0, len(edges) - 2)

    rows = []
    for i in range(len(edges) - 1):
        mask = idx == i
        if not np.any(mask):
            continue
        vals = y_vals[mask]
        rows.append({
            "t_ms": float(edges[i] + 0.5 * bin_ms),
            "flux_median": float(np.median(vals)),
            "flux_p25": float(np.percentile(vals, 25)),
            "flux_p75": float(np.percentile(vals, 75)),
            "count": int(np.sum(mask)),
        })
    return rows


def interpolate_crossing(rows, thr, direction="rising"):
    if not rows:
        return None
    t = np.asarray([r["t_ms"] for r in rows], dtype=float)
    y = np.asarray([r["flux_median"] for r in rows], dtype=float)

    if direction == "rising":
        for i in range(1, len(y)):
            if y[i - 1] < thr <= y[i]:
                y0, y1 = y[i - 1], y[i]
                if y1 == y0:
                    return float(t[i])
                frac = (thr - y0) / (y1 - y0)
                return float(t[i - 1] + frac * (t[i] - t[i - 1]))
    else:
        for i in range(1, len(y)):
            if y[i - 1] > thr >= y[i]:
                y0, y1 = y[i - 1], y[i]
                if y1 == y0:
                    return float(t[i])
                frac = (y0 - thr) / (y0 - y1)
                return float(t[i - 1] + frac * (t[i] - t[i - 1]))
    return None


def aggregate_metric_rows(rows):
    by_metric = {}
    for row in rows:
        value = row.get("value_ms")
        if value is None or math.isnan(value):
            continue
        by_metric.setdefault(row["metric"], []).append(float(value))

    out = []
    for metric in sorted(by_metric):
        vals = np.asarray(by_metric[metric], dtype=float)
        out.append({
            "metric": metric,
            "n": int(len(vals)),
            "value_ms_mean": float(np.mean(vals)),
            "value_ms_median": float(np.median(vals)),
            "value_ms_std": float(np.std(vals)),
        })
    return out


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def plot_command_curve(rows, out_path):
    if plt is None or not rows:
        return
    x = np.asarray([r["cmd_ms"] for r in rows], dtype=float)
    y = np.asarray([r["eff_ms_mean"] for r in rows], dtype=float)
    y_std = np.asarray([r["eff_ms_std"] for r in rows], dtype=float)
    y_med = np.asarray([r["eff_ms_median"] for r in rows], dtype=float)

    plt.figure(figsize=(9, 6))
    plt.errorbar(x, y, yerr=y_std, fmt="o", label="mean +/- std", alpha=0.8)
    plt.plot(x, y_med, "-", label="median")
    plt.plot(x, x, "--", label="cmd = eff")
    plt.xlabel("Commanded duration (ms)")
    plt.ylabel("Metric-equivalent exposure (ms)")
    plt.title("Combined Command-to-Effective Curve")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def plot_flux_curve(rows, out_path, title, xlabel):
    if plt is None or not rows:
        return
    t = np.asarray([r["t_ms"] for r in rows], dtype=float)
    med = np.asarray([r["flux_median"] for r in rows], dtype=float)
    p25 = np.asarray([r["flux_p25"] for r in rows], dtype=float)
    p75 = np.asarray([r["flux_p75"] for r in rows], dtype=float)

    plt.figure(figsize=(9, 6))
    plt.plot(t, med, label="median")
    plt.fill_between(t, p25, p75, alpha=0.2, label="IQR")
    plt.xlabel(xlabel)
    plt.ylabel("Normalized flux")
    plt.title(title)
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def main():
    parser = argparse.ArgumentParser(description="Build combined shutter reference curves from analysis outputs.")
    parser.add_argument("--results-dirs", nargs="+", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--bin-ms", type=float, default=1.0)
    parser.add_argument("--closing-min-cmd-ms", type=float, default=150.0)
    parser.add_argument("--closing-peak-min", type=float, default=0.95)
    parser.add_argument("--pre-open-ms", type=float, default=5.0)
    parser.add_argument("--post-open-ms", type=float, default=100.0)
    parser.add_argument("--pre-close-ms", type=float, default=40.0)
    parser.add_argument("--post-close-ms", type=float, default=80.0)
    parser.add_argument("--onset-threshold", type=float, default=0.15)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    summary_rows = load_summary_rows(args.results_dirs)
    summary_map = {(r["video"], r["pulse_id"]): r for r in summary_rows}
    trace_samples = load_trace_samples(args.results_dirs, summary_map)
    pulse_traces = group_trace_samples(trace_samples)

    cmd_rows = aggregate_command_effective(summary_rows)
    opening_points, opening_metric_rows = build_opening_curve(
        pulse_traces,
        summary_map,
        onset_threshold=args.onset_threshold,
        pre_ms=args.pre_open_ms,
        post_ms=args.post_open_ms,
    )
    retraction_points, retraction_metric_rows = build_retraction_curve(
        pulse_traces,
        summary_map,
        closing_min_cmd_ms=args.closing_min_cmd_ms,
        closing_peak_min=args.closing_peak_min,
        pre_ms=args.pre_close_ms,
        post_ms=args.post_close_ms,
        onset_threshold=args.onset_threshold,
    )

    opening_rows = bin_points(opening_points, bin_ms=args.bin_ms)
    retraction_rows = bin_points(retraction_points, bin_ms=args.bin_ms)

    pooled_summary = [
        {"metric": "opening_curve_crosses_0.15_ms", "value_ms": interpolate_crossing(opening_rows, args.onset_threshold, "rising")},
        {"metric": "opening_curve_crosses_0.50_ms", "value_ms": interpolate_crossing(opening_rows, 0.50, "rising")},
        {"metric": "opening_curve_crosses_0.95_ms", "value_ms": interpolate_crossing(opening_rows, 0.95, "rising")},
        {"metric": "retraction_curve_crosses_0.95_ms", "value_ms": interpolate_crossing(retraction_rows, 0.95, "falling")},
        {"metric": "retraction_curve_crosses_0.50_ms", "value_ms": interpolate_crossing(retraction_rows, 0.50, "falling")},
        {"metric": "retraction_curve_crosses_0.15_ms", "value_ms": interpolate_crossing(retraction_rows, args.onset_threshold, "falling")},
        {"metric": "retraction_curve_crosses_0.02_ms", "value_ms": interpolate_crossing(retraction_rows, 0.02, "falling")},
    ]
    summary_rows_out = aggregate_metric_rows(opening_metric_rows + retraction_metric_rows + pooled_summary)

    write_csv(
        os.path.join(args.out_dir, "combined_command_effective.csv"),
        ["cmd_ms", "n", "eff_ms_mean", "eff_ms_median", "eff_ms_std"],
        cmd_rows,
    )
    write_csv(
        os.path.join(args.out_dir, "opening_reference_curve.csv"),
        ["t_ms", "flux_median", "flux_p25", "flux_p75", "count"],
        opening_rows,
    )
    write_csv(
        os.path.join(args.out_dir, "retraction_reference_curve.csv"),
        ["t_ms", "flux_median", "flux_p25", "flux_p75", "count"],
        retraction_rows,
    )
    write_csv(
        os.path.join(args.out_dir, "reference_summary.csv"),
        ["metric", "n", "value_ms_mean", "value_ms_median", "value_ms_std"],
        summary_rows_out,
    )

    plot_command_curve(cmd_rows, os.path.join(args.out_dir, "combined_command_effective.png"))
    plot_flux_curve(
        opening_rows,
        os.path.join(args.out_dir, "opening_reference_curve.png"),
        title="Combined Opening Reference Curve",
        xlabel="Time from 15% rise crossing (ms)",
    )
    plot_flux_curve(
        retraction_rows,
        os.path.join(args.out_dir, "retraction_reference_curve.png"),
        title="Combined Retraction Reference Curve",
        xlabel="Time from 95% fall crossing (ms)",
    )

    print(f"Wrote combined reference outputs to: {args.out_dir}")


if __name__ == "__main__":
    main()
