import argparse
import csv
import os
import random


BASE_SETS = {
    # Tight around first-light onset.
    "onset_fine": [14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24],
    # Dense coverage between onset and full retraction.
    "transient_coarse": list(range(16, 81, 2)),
    # Tight around full-retraction knee/plateau.
    "plateau_fine": [72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82],
    # Sparse long-duration mapping.
    "long_tail": [120, 150, 200, 250, 300, 400, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000],
}


def make_passes(values, passes, rng):
    out = []
    for _ in range(passes):
        row = list(values)
        rng.shuffle(row)
        out.append(row)
    return out


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Create shuffled duration pass plans for shutter calibration.")
    parser.add_argument("--passes", type=int, default=10, help="Number of shuffled passes per set.")
    parser.add_argument("--gap-s", type=float, default=3.0, help="Recommended inter-pulse gap.")
    parser.add_argument("--seed", type=int, default=20260307, help="RNG seed for reproducible shuffles.")
    parser.add_argument("--out-dir", default="plans", help="Output directory for CSV plans.")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    rng = random.Random(args.seed)

    all_unique = sorted({v for values in BASE_SETS.values() for v in values})
    sets = dict(BASE_SETS)
    sets["all_unique"] = all_unique

    manifest_rows = []
    long_rows = []

    for set_name, values in sets.items():
        passes = make_passes(values, args.passes, rng)
        for pass_idx, order in enumerate(passes, start=1):
            rec_id = f"{set_name}_pass{pass_idx:02d}"
            dur_csv = ",".join(str(v) for v in order)
            manifest_rows.append({
                "recording_id": rec_id,
                "set_name": set_name,
                "pass_index": pass_idx,
                "pulse_count": len(order),
                "gap_s": args.gap_s,
                "durations_list": dur_csv,
                "seed": args.seed,
            })

            for pulse_idx, ms in enumerate(order, start=1):
                long_rows.append({
                    "recording_id": rec_id,
                    "set_name": set_name,
                    "pass_index": pass_idx,
                    "pulse_index": pulse_idx,
                    "commanded_ms": ms,
                })

    manifest_path = os.path.join(args.out_dir, "shutter_pass_manifest.csv")
    long_path = os.path.join(args.out_dir, "shutter_pass_longform.csv")

    write_csv(
        manifest_path,
        ["recording_id", "set_name", "pass_index", "pulse_count", "gap_s", "durations_list", "seed"],
        manifest_rows,
    )
    write_csv(
        long_path,
        ["recording_id", "set_name", "pass_index", "pulse_index", "commanded_ms"],
        long_rows,
    )

    print(f"Wrote manifest: {manifest_path}")
    print(f"Wrote longform: {long_path}")
    print(f"Sets: {', '.join(sets.keys())}")
    print(f"Passes per set: {args.passes}")


if __name__ == "__main__":
    main()
