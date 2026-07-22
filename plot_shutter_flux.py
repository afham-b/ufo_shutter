import argparse
import csv
import glob
import os
import sys

import numpy as np

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception as e:
    print("[ERROR] matplotlib is required for plotting.")
    print("Install with: python -m pip install matplotlib")
    raise


def read_calibration_table(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                rows.append({
                    "cmd_ms": int(row["cmd_ms"]),
                    "n": int(row["n"]),
                    "eff_ms_mean": float(row["eff_ms_mean"]),
                    "eff_ms_median": float(row["eff_ms_median"]),
                    "eff_ms_std": float(row["eff_ms_std"]),
                })
            except Exception:
                continue
    return rows


def read_aligned_traces(paths):
    data = {}
    for path in paths:
        with open(path, "r", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    cmd = int(row["cmd_ms"])
                    t_ms = float(row["t_ms_from_exp_start"])
                    flux = float(row["norm_flux"])
                except Exception:
                    continue
                data.setdefault(cmd, []).append((t_ms, flux))
    return data


def choose_commands(commands, n_select=6):
    if not commands:
        return []
    cmds = sorted(commands)
    if len(cmds) <= n_select:
        return cmds
    idx = np.linspace(0, len(cmds) - 1, n_select).astype(int)
    return [cmds[i] for i in idx]


def bin_time_series(points, step_ms=1.0):
    t = np.array([p[0] for p in points], dtype=float)
    y = np.array([p[1] for p in points], dtype=float)

    t_min = float(np.floor(np.min(t)))
    t_max = float(np.ceil(np.max(t)))
    bins = np.arange(t_min, t_max + step_ms, step_ms)
    if bins.size < 2:
        return np.array([]), np.array([]), np.array([])

    bin_idx = np.floor((t - t_min) / step_ms).astype(int)
    bin_idx = np.clip(bin_idx, 0, len(bins) - 2)

    med = np.full(len(bins) - 1, np.nan, dtype=float)
    p25 = np.full(len(bins) - 1, np.nan, dtype=float)
    p75 = np.full(len(bins) - 1, np.nan, dtype=float)

    for i in range(len(bins) - 1):
        mask = bin_idx == i
        if not np.any(mask):
            continue
        vals = y[mask]
        med[i] = float(np.median(vals))
        p25[i] = float(np.percentile(vals, 25))
        p75[i] = float(np.percentile(vals, 75))

    t_mid = bins[:-1] + 0.5 * step_ms
    return t_mid, med, (p25, p75)


def plot_calibration(rows, out_path):
    if not rows:
        print("[WARN] No calibration table rows to plot.")
        return
    rows = sorted(rows, key=lambda r: r["cmd_ms"])
    x = np.array([r["cmd_ms"] for r in rows])
    y = np.array([r["eff_ms_mean"] for r in rows])
    y_med = np.array([r["eff_ms_median"] for r in rows])
    y_std = np.array([r["eff_ms_std"] for r in rows])

    plt.figure(figsize=(9, 6))
    plt.errorbar(x, y, yerr=y_std, fmt="o", label="mean ± std", alpha=0.8)
    plt.plot(x, y_med, "-", label="median")
    plt.plot(x, x, "--", label="ideal (cmd = eff)")
    plt.xlabel("Commanded exposure (ms)")
    plt.ylabel("Effective exposure (ms)")
    plt.title("Shutter Calibration Curve")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def plot_flux_curves(data, out_path, step_ms=1.0, n_select=6):
    if not data:
        print("[WARN] No aligned traces found for flux curves.")
        return
    selected = choose_commands(list(data.keys()), n_select=n_select)

    plt.figure(figsize=(10, 6))
    for cmd in selected:
        t_mid, med, (p25, p75) = bin_time_series(data[cmd], step_ms=step_ms)
        if t_mid.size == 0:
            continue
        plt.plot(t_mid, med, label=f"{cmd} ms")
        plt.fill_between(t_mid, p25, p75, alpha=0.15)

    plt.xlabel("Time from command start (ms)")
    plt.ylabel("Normalized flux")
    plt.title("Median Flux vs Time (Selected Commands)")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def main():
    parser = argparse.ArgumentParser(description="Plot shutter flux calibration outputs.")
    parser.add_argument("--results-dir", default="results_flux_new")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--step-ms", type=float, default=1.0)
    parser.add_argument("--n-select", type=int, default=6)
    parser.add_argument("--trace-glob", default="*_aligned_trace.csv")

    args = parser.parse_args()

    results_dir = args.results_dir
    out_dir = args.out_dir or os.path.join(results_dir, "plots")
    os.makedirs(out_dir, exist_ok=True)

    calib_path = os.path.join(results_dir, "calibration_table.csv")
    rows = read_calibration_table(calib_path)
    plot_calibration(rows, os.path.join(out_dir, "calibration_curve.png"))

    trace_paths = glob.glob(os.path.join(results_dir, args.trace_glob))
    data = read_aligned_traces(trace_paths)
    plot_flux_curves(data, os.path.join(out_dir, "flux_curves.png"), step_ms=args.step_ms, n_select=args.n_select)

    print(f"Wrote plots to: {out_dir}")


if __name__ == "__main__":
    main()
