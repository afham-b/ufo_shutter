import argparse
import csv
import math
import os

import numpy as np

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    plt = None


def read_combined_rows(path, y_column):
    rows = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "cmd_ms": float(row["cmd_ms"]),
                "n": int(row["n"]),
                "y_obs": float(row[y_column]),
                "y_mean": float(row["eff_ms_mean"]),
                "y_median": float(row["eff_ms_median"]),
                "y_std": float(row["eff_ms_std"]),
            })
    return rows


def isotonic_increasing(y, w):
    blocks = []
    for idx, (yy, ww) in enumerate(zip(y, w)):
        block = {
            "start": idx,
            "end": idx,
            "sum_w": float(ww),
            "sum_yw": float(yy * ww),
        }
        blocks.append(block)
        while len(blocks) >= 2:
            a = blocks[-2]
            b = blocks[-1]
            mean_a = a["sum_yw"] / a["sum_w"]
            mean_b = b["sum_yw"] / b["sum_w"]
            if mean_a <= mean_b:
                break
            merged = {
                "start": a["start"],
                "end": b["end"],
                "sum_w": a["sum_w"] + b["sum_w"],
                "sum_yw": a["sum_yw"] + b["sum_yw"],
            }
            blocks[-2:] = [merged]

    out = np.zeros(len(y), dtype=float)
    for block in blocks:
        mean = block["sum_yw"] / block["sum_w"]
        out[block["start"]:block["end"] + 1] = mean
    return out


def interp_piecewise_linear(x, xp, yp):
    x = float(x)
    xp = np.asarray(xp, dtype=float)
    yp = np.asarray(yp, dtype=float)
    if len(xp) == 1:
        return float(yp[0])

    if x <= xp[0]:
        x0, x1 = xp[0], xp[1]
        y0, y1 = yp[0], yp[1]
    elif x >= xp[-1]:
        x0, x1 = xp[-2], xp[-1]
        y0, y1 = yp[-2], yp[-1]
    else:
        i = int(np.searchsorted(xp, x, side="right")) - 1
        x0, x1 = xp[i], xp[i + 1]
        y0, y1 = yp[i], yp[i + 1]

    if x1 == x0:
        return float(y0)
    return float(y0 + (x - x0) * (y1 - y0) / (x1 - x0))


def inverse_interp_monotone(y_target, xp_cmd, yp_eff):
    y_target = float(y_target)
    xp_cmd = np.asarray(xp_cmd, dtype=float)
    yp_eff = np.asarray(yp_eff, dtype=float)

    if len(xp_cmd) == 1:
        return float(xp_cmd[0])

    if y_target <= yp_eff[0]:
        return float(xp_cmd[0])

    if y_target >= yp_eff[-1]:
        for i in range(len(yp_eff) - 2, -1, -1):
            if yp_eff[i + 1] > yp_eff[i]:
                y0, y1 = yp_eff[i], yp_eff[i + 1]
                x0, x1 = xp_cmd[i], xp_cmd[i + 1]
                return float(x0 + (y_target - y0) * (x1 - x0) / (y1 - y0))
        return float(xp_cmd[-1])

    i = int(np.searchsorted(yp_eff, y_target, side="left"))
    if i <= 0:
        return float(xp_cmd[0])

    y0, y1 = yp_eff[i - 1], yp_eff[i]
    x0, x1 = xp_cmd[i - 1], xp_cmd[i]

    if y1 == y0:
        return float(x0)
    return float(x0 + (y_target - y0) * (x1 - x0) / (y1 - y0))


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def plot_master_curve(cmds, y_obs, y_fit, forward_rows, out_path):
    if plt is None:
        return

    fx = np.asarray([r["cmd_ms"] for r in forward_rows], dtype=float)
    fy = np.asarray([r["delivered_metric_ms"] for r in forward_rows], dtype=float)

    plt.figure(figsize=(10, 6))
    plt.plot(fx, fy, "-", lw=2, label="monotone master fit")
    plt.plot(cmds, y_obs, "o", alpha=0.7, label="combined observed points")
    plt.plot(cmds, y_fit, "s", ms=4, label="isotonic knots")
    plt.plot(fx, fx, "--", alpha=0.5, label="cmd = delivered")
    plt.xlabel("Commanded duration (ms)")
    plt.ylabel("Delivered metric exposure (ms)")
    plt.title("Monotone Master Shutter Calibration")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()


def main():
    parser = argparse.ArgumentParser(description="Fit a monotone master shutter calibration curve.")
    parser.add_argument(
        "--combined-csv",
        default="results_flux_reference_useful10p15/combined_command_effective.csv",
        help="Combined command/effective CSV from the analysis pipeline.",
    )
    parser.add_argument(
        "--out-dir",
        default="results_flux_reference_useful10p15",
        help="Directory for master curve outputs.",
    )
    parser.add_argument(
        "--y-column",
        choices=["eff_ms_mean", "eff_ms_median"],
        default="eff_ms_median",
        help="Observed column to fit.",
    )
    parser.add_argument(
        "--origin-weight",
        type=float,
        default=32.0,
        help="Weight for the physical anchor point (0 ms command -> 0 ms delivered).",
    )
    parser.add_argument(
        "--max-cmd-ms",
        type=int,
        default=4000,
        help="Maximum commanded duration for dense lookup export.",
    )
    parser.add_argument(
        "--max-target-ms",
        type=int,
        default=4000,
        help="Maximum target delivered duration for inverse lookup export.",
    )
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    rows = read_combined_rows(args.combined_csv, args.y_column)
    if not rows:
        raise RuntimeError(f"No rows found in {args.combined_csv}")

    cmds = [0.0] + [r["cmd_ms"] for r in rows]
    y_obs = [0.0] + [r["y_obs"] for r in rows]
    weights = [float(args.origin_weight)] + [float(max(1, r["n"])) for r in rows]

    cmds_arr = np.asarray(cmds, dtype=float)
    y_obs_arr = np.asarray(y_obs, dtype=float)
    weights_arr = np.asarray(weights, dtype=float)
    y_fit_arr = isotonic_increasing(y_obs_arr, weights_arr)

    point_rows = []
    point_rows.append({
        "cmd_ms": 0,
        "n": int(args.origin_weight),
        "observed_metric_ms": 0.0,
        "fitted_metric_ms": float(y_fit_arr[0]),
    })
    for i, row in enumerate(rows, start=1):
        point_rows.append({
            "cmd_ms": int(round(cmds_arr[i])),
            "n": int(row["n"]),
            "observed_metric_ms": float(y_obs_arr[i]),
            "fitted_metric_ms": float(y_fit_arr[i]),
        })

    forward_rows = []
    for cmd_ms in range(0, int(args.max_cmd_ms) + 1):
        delivered = interp_piecewise_linear(cmd_ms, cmds_arr, y_fit_arr)
        forward_rows.append({
            "cmd_ms": cmd_ms,
            "delivered_metric_ms": delivered,
        })

    inverse_rows = []
    for target_ms in range(0, int(args.max_target_ms) + 1):
        cmd_ms = inverse_interp_monotone(target_ms, cmds_arr, y_fit_arr)
        inverse_rows.append({
            "target_metric_ms": target_ms,
            "command_ms": cmd_ms,
        })

    points_path = os.path.join(args.out_dir, "master_curve_points.csv")
    forward_path = os.path.join(args.out_dir, "master_forward_lookup.csv")
    inverse_path = os.path.join(args.out_dir, "master_inverse_lookup.csv")
    plot_path = os.path.join(args.out_dir, "master_curve.png")

    write_csv(
        points_path,
        ["cmd_ms", "n", "observed_metric_ms", "fitted_metric_ms"],
        point_rows,
    )
    write_csv(
        forward_path,
        ["cmd_ms", "delivered_metric_ms"],
        forward_rows,
    )
    write_csv(
        inverse_path,
        ["target_metric_ms", "command_ms"],
        inverse_rows,
    )
    plot_master_curve(cmds_arr, y_obs_arr, y_fit_arr, forward_rows, plot_path)

    print(f"Wrote master curve points: {points_path}")
    print(f"Wrote forward lookup: {forward_path}")
    print(f"Wrote inverse lookup: {inverse_path}")
    if plt is not None:
        print(f"Wrote plot: {plot_path}")


if __name__ == "__main__":
    main()
