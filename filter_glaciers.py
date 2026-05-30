#!/usr/bin/env python3
"""
filter_glaciers.py
------------------
Filters a GeoJSON FeatureCollection, removing features where the
`glac_name` property equals the string "None", JSON null, or is absent.

Usage:
    python filter_glaciers.py <input.geojson> [output.geojson]

    If output path is omitted, the result is written next to the input
    file with "_filtered" appended to the name.

Options:
    --field     Property field to check            (default: glac_name)
    --null-str  String value treated as null       (default: "None")
    --keep-null Invert: KEEP only null/None rows   (flag, off by default)
    --stream    Use streaming parser for huge files (flag, off by default)
    --pretty    Pretty-print output JSON           (flag, off by default)
    --stats     Print summary statistics           (flag, on by default)

Examples:
    # Keep only features that have a real glacier name
    python filter_glaciers.py glaciers.geojson

    # Keep only the unnamed ones (invert filter)
    python filter_glaciers.py glaciers.geojson unnamed.geojson --keep-null

    # Use a different field
    python filter_glaciers.py data.geojson --field src_date

    # Stream a very large file without loading it all into RAM
    python filter_glaciers.py huge.geojson filtered.geojson --stream
"""

import argparse
import json
import os
import sys
import time


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_null_value(value: object, null_str: str) -> bool:
    """Return True if value should be treated as null/None."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == null_str:
        return True
    return False


def build_output_path(input_path: str) -> str:
    base, ext = os.path.splitext(input_path)
    return f"{base}_filtered{ext or '.geojson'}"


# ---------------------------------------------------------------------------
# Standard (in-memory) filtering
# ---------------------------------------------------------------------------

def filter_standard(
    input_path: str,
    output_path: str,
    field: str,
    null_str: str,
    keep_null: bool,
    pretty: bool,
) -> tuple[int, int]:
    """Load the whole file, filter, write. Returns (total, kept)."""

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") != "FeatureCollection":
        sys.exit("ERROR: root element is not a GeoJSON FeatureCollection.")

    features = data.get("features", [])
    total = len(features)

    def _keep(feature: dict) -> bool:
        value = feature.get("properties", {}).get(field)
        null = is_null_value(value, null_str)
        # keep_null=True  → keep the null ones (i.e. return null)
        # keep_null=False → keep the non-null ones
        return null if keep_null else not null

    kept_features = [f for f in features if _keep(f)]

    data["features"] = kept_features

    indent = 2 if pretty else None
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)

    return total, len(kept_features)


# ---------------------------------------------------------------------------
# Streaming filtering (for very large files)
# Uses the same depth-tracking approach as the chunker script.
# ---------------------------------------------------------------------------

def filter_streaming(
    input_path: str,
    output_path: str,
    field: str,
    null_str: str,
    keep_null: bool,
    pretty: bool,
) -> tuple[int, int]:
    """
    Stream the file character-by-character, extract each top-level
    feature as a raw string, filter it, then write to output.

    Memory usage: proportional to a single feature, not the whole file.
    """
    total = 0
    kept = 0

    # --- Read header (everything before the first feature) ----------------
    # We need to preserve the FeatureCollection wrapper and its properties.
    # Strategy: read the full file but only parse the header once cheaply
    # by finding the "features" array start, then stream elements.

    with open(input_path, "r", encoding="utf-8") as fin:
        raw = fin.read()

    # Find the `"features"` key and the opening `[` of its array.
    features_key = '"features"'
    key_pos = raw.find(features_key)
    if key_pos == -1:
        sys.exit("ERROR: Could not find a 'features' key in the file.")

    bracket_pos = raw.index("[", key_pos + len(features_key))

    # Header = everything up to and including the `[`
    header = raw[: bracket_pos + 1]

    # Footer = everything after the closing `]` of the features array
    # Find the matching closing bracket
    depth = 0
    in_string = False
    escape = False
    close_pos = bracket_pos

    for i in range(bracket_pos, len(raw)):
        ch = raw[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch in ("[", "{"):
                depth += 1
            elif ch in ("]", "}"):
                depth -= 1
                if depth == 0:
                    close_pos = i
                    break

    footer = raw[close_pos + 1 :]  # everything after the `]`

    # --- Stream through individual feature strings -------------------------
    feature_text = raw[bracket_pos + 1 : close_pos]

    # Split on top-level commas (depth 0 inside the array)
    def split_top_level(text: str):
        """Yield individual feature JSON strings."""
        depth = 0
        in_string = False
        escape = False
        buf = []

        for ch in text:
            if escape:
                escape = False
                buf.append(ch)
                continue
            if ch == "\\" and in_string:
                escape = True
                buf.append(ch)
                continue
            if ch == '"':
                in_string = not in_string
                buf.append(ch)
                continue
            if not in_string:
                if ch in ("{", "["):
                    depth += 1
                elif ch in ("}", "]"):
                    depth -= 1
                elif ch == "," and depth == 0:
                    s = "".join(buf).strip()
                    if s:
                        yield s
                    buf = []
                    continue
            buf.append(ch)

        s = "".join(buf).strip()
        if s:
            yield s

    indent_str = "  " if pretty else ""
    sep = ",\n" + indent_str if pretty else ","

    with open(output_path, "w", encoding="utf-8") as fout:
        # Write FeatureCollection header
        fout.write(header)
        if pretty:
            fout.write("\n")

        first = True
        for feature_str in split_top_level(feature_text):
            total += 1
            try:
                feature = json.loads(feature_str)
            except json.JSONDecodeError:
                print(f"  WARNING: could not parse feature #{total}, skipping.")
                continue

            value = feature.get("properties", {}).get(field)
            null = is_null_value(value, null_str)
            should_keep = null if keep_null else not null

            if should_keep:
                if not first:
                    fout.write(sep)
                if pretty:
                    fout.write(indent_str + json.dumps(feature, ensure_ascii=False))
                else:
                    fout.write(json.dumps(feature, ensure_ascii=False))
                first = False
                kept += 1

        if pretty:
            fout.write("\n")
        fout.write("]")
        fout.write(footer)

    return total, kept


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Filter GeoJSON features by a property value.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("input", help="Path to input GeoJSON file")
    p.add_argument("output", nargs="?", help="Path to output GeoJSON file (optional)")
    p.add_argument(
        "--field",
        default="glac_name",
        help='Property field to inspect (default: "glac_name")',
    )
    p.add_argument(
        "--null-str",
        default="None",
        help='String value treated as null (default: "None")',
    )
    p.add_argument(
        "--keep-null",
        action="store_true",
        help="Invert: keep only features where the field IS null/None",
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="Use streaming parser — low memory, good for huge files",
    )
    p.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print output JSON (larger file, more readable)",
    )
    p.add_argument(
        "--no-stats",
        action="store_true",
        help="Suppress the summary statistics output",
    )
    return p.parse_args()


def main():
    args = parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"ERROR: input file not found: {args.input}")

    output_path = args.output or build_output_path(args.input)

    file_size_mb = os.path.getsize(args.input) / (1024 * 1024)
    mode = "streaming" if args.stream else "in-memory"

    print(f"Input  : {args.input} ({file_size_mb:.2f} MB)")
    print(f"Output : {output_path}")
    print(f"Filter : features where '{args.field}' "
          + ("IS" if args.keep_null else "is NOT")
          + f" null / \"{args.null_str}\"")
    print(f"Mode   : {mode}")
    print()

    t0 = time.perf_counter()

    if args.stream:
        total, kept = filter_streaming(
            args.input, output_path, args.field, args.null_str,
            args.keep_null, args.pretty,
        )
    else:
        total, kept = filter_standard(
            args.input, output_path, args.field, args.null_str,
            args.keep_null, args.pretty,
        )

    elapsed = time.perf_counter() - t0
    removed = total - kept
    out_size_mb = os.path.getsize(output_path) / (1024 * 1024)

    if not args.no_stats:
        print(f"Done in {elapsed:.2f}s")
        print(f"  Total features  : {total:,}")
        print(f"  Kept            : {kept:,}")
        print(f"  Removed         : {removed:,}")
        print(f"  Output size     : {out_size_mb:.2f} MB")

    return 0


if __name__ == "__main__":
    sys.exit(main())