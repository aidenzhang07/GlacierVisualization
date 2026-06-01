#!/usr/bin/env python3
"""
split_glaciers.py
-----------------
Splits one or more large glacier GeoJSON files into per-glacier FeatureCollections
grouped by ID prefix. Deduplicates observations on (glac_id, src_date).
Automatically generates a frontend-ready manifest.json.

Memory: Constant during streaming. Grouping uses ~200-400 MB RAM for 400K features.
Dedup set: ~30 MB RAM for 400K unique (glac_id, src_date) tuples.
Output: data/<prefix>/<glac_id>.geojson + data/manifest.json

Usage:
    # Split multiple files, dedup, auto-generate manifest
    python split_glaciers.py current.geojson historical.geojson glacier_data/

    # Skip glaciers with only 1 observation
    python split_glaciers.py *.geojson glacier_data/ --skip-single

    # Generate manifest from an already-split directory
    python split_glaciers.py --manifest-only glacier_data/
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from typing import Generator, Any, Dict, List, Set, Tuple

# ---------------------------------------------------------------------------
# Streaming Core (Robust version)
# ---------------------------------------------------------------------------
def _find_features_bounds(path: str) -> tuple[int, int, str, str]:
    """Locate the '[' and ']' wrapping the 'features' array."""
    PAGE = 64 * 1024
    features_key = b'"features"'
    key_pos_byte = -1
    with open(path, "rb") as f:
        pos = 0
        buf = b""
        while True:
            chunk = f.read(PAGE)
            if not chunk: break
            combined = buf + chunk
            idx = combined.find(features_key)
            if idx != -1:
                key_pos_byte = pos - len(buf) + idx
                break
            buf = combined[-(len(features_key) - 1):]
            pos += len(chunk)
    if key_pos_byte == -1:
        sys.exit(f"ERROR: Could not find a 'features' key in {path}")

    depth = 0
    in_string = False
    escape = False
    bracket_byte = -1
    close_byte = -1
    with open(path, "rb") as f:
        f.seek(key_pos_byte)
        byte_offset = key_pos_byte
        while True:
            chunk = f.read(PAGE)
            if not chunk: break
            text = chunk.decode("utf-8", errors="replace")
            for i, ch in enumerate(text):
                abs_pos = byte_offset + i
                if escape: escape = False; continue
                if ch == "\\" and in_string: escape = True; continue
                if ch == '"': in_string = not in_string; continue
                if in_string: continue
                if bracket_byte == -1:
                    if ch == "[": bracket_byte = abs_pos; depth = 1; continue
                if ch in ("[", "{"): depth += 1
                elif ch in ("]", "}"):
                    depth -= 1
                    if depth == 0: close_byte = abs_pos; break
            if close_byte != -1: break
            byte_offset += len(chunk)
    if bracket_byte == -1 or close_byte == -1:
        sys.exit(f"ERROR: Could not locate the features array bounds in {path}")
    with open(path, "r", encoding="utf-8") as f:
        header = f.read(bracket_byte + 1)
        f.seek(close_byte + 1)
        footer = f.read()
    return bracket_byte, close_byte, header, footer

def iter_features(path: str) -> Generator[dict, None, None]:
    """Yield features one-by-one with constant memory."""
    PAGE = 64 * 1024
    bracket_byte, _, _, _ = _find_features_bounds(path)
    depth = 0
    in_string = False
    escape = False
    buf: list[str] = []
    
    with open(path, "r", encoding="utf-8") as f:
        f.seek(bracket_byte + 1)
        while True:
            chunk = f.read(PAGE)
            if not chunk: break
            for ch in chunk:
                if escape:
                    escape = False
                    if depth > 0: buf.append(ch)
                    continue
                if ch == "\\" and in_string:
                    escape = True
                    if depth > 0: buf.append(ch)
                    continue
                if ch == '"':
                    in_string = not in_string
                    if depth > 0: buf.append(ch)
                    continue
                if in_string:
                    if depth > 0: buf.append(ch)
                    continue
                if depth == 0:
                    if ch == '{': depth = 1; buf = [ch]
                    continue
                else:
                    buf.append(ch)
                    if ch in ('{', '['): depth += 1
                    elif ch in ('}', ']'):
                        depth -= 1
                        if depth < 0: return
                        if depth == 0:
                            feat_str = "".join(buf).strip()
                            buf = []
                            if feat_str:
                                try: yield json.loads(feat_str)
                                except json.JSONDecodeError as e:
                                    print(f"  WARNING: skipping unparseable feature in {path}: {e}", file=sys.stderr)
            else: continue
            break

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_metadata(path: str) -> tuple[str, Any]:
    """Safely extract top-level GeoJSON metadata without loading the full file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(8192)
        head_safe = re.sub(r'"features"\s*:\s*\[.*', '"features": []', head, flags=re.DOTALL)
        meta = json.loads(head_safe)
        return meta.get("name", "glacier_observations"), meta.get("crs")
    except Exception:
        return "glacier_observations", None

def write_glacier_file(out_dir: str, glac_id: str, feats: list[dict], 
                       name: str, crs: Any, pretty: bool) -> str:
    """Write a single glacier's FeatureCollection to disk, sorted by src_date."""
    feats.sort(key=lambda f: f.get("properties", {}).get("src_date", ""))
    fc = {"type": "FeatureCollection", "name": name, "features": feats}
    if crs: fc["crs"] = crs
        
    prefix_dir = os.path.join(out_dir, glac_id[:4])
    os.makedirs(prefix_dir, exist_ok=True)
    out_path = os.path.join(prefix_dir, f"{glac_id}.geojson")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, indent=2 if pretty else None, ensure_ascii=False)
    return out_path

def _build_manifest_entry(glac_id: str, feats: list[dict]) -> dict:
    """Extract frontend-friendly metadata."""
    feats.sort(key=lambda f: f.get("properties", {}).get("src_date", ""))
    props = feats[0].get("properties", {})
    return {
        "path": f"{glac_id[:4]}/{glac_id}.geojson",
        "count": len(feats),
        "name": props.get("glac_name", "Unknown"),
        "date_range": [
            feats[0]["properties"].get("src_date"),
            feats[-1]["properties"].get("src_date")
        ]
    }

def write_manifest(out_dir: str, manifest_data: dict, manifest_name: str = "manifest.json"):
    """Write the collected manifest data to disk."""
    manifest = {
        "type": "GlacierManifest",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_glaciers": len(manifest_data),
        "glaciers": manifest_data
    }
    manifest_path = os.path.join(out_dir, manifest_name)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return manifest_path

def generate_manifest_from_dir(out_dir: str, manifest_name: str = "manifest.json"):
    """Scan an existing split directory and generate manifest.json."""
    manifest_data = {}
    count = 0
    print(f"Scanning {out_dir} for existing glacier files...")
    for root, _, files in os.walk(out_dir):
        for f in files:
            if not f.endswith(".geojson"): continue
            glac_id = f.replace(".geojson", "")
            filepath = os.path.join(root, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    fc = json.load(fh)
                feats = fc.get("features", [])
                if not feats: continue
                manifest_data[glac_id] = _build_manifest_entry(glac_id, feats)
                count += 1
            except Exception as e:
                print(f"  Warning: skipping {filepath}: {e}", file=sys.stderr)
    path = write_manifest(out_dir, manifest_data, manifest_name)
    print(f"✅ Manifest generated: {path} ({count} glaciers)")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Split one or more glacier GeoJSON files into per-glacier files + generate manifest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_files", nargs="*", help="Input GeoJSON file(s)")
    parser.add_argument("output_dir", help="Directory for split files / source for manifest scan")
    parser.add_argument("--prefix-len", type=int, default=4, help="Length of ID prefix for folders (default: 4)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print output JSON")
    parser.add_argument("--skip-single", action="store_true", help="Skip glaciers with only 1 observation")
    parser.add_argument("--manifest-only", action="store_true", help="Skip splitting. Only scan output_dir and generate manifest")
    parser.add_argument("--no-manifest", action="store_true", help="During split, do not generate manifest.json")
    parser.add_argument("--manifest-name", default="manifest.json", help="Output manifest filename")
    args = parser.parse_args()

    # Manifest-only mode
    if args.manifest_only:
        if not os.path.isdir(args.output_dir):
            sys.exit(f"ERROR: {args.output_dir} is not a valid directory")
        generate_manifest_from_dir(args.output_dir, args.manifest_name)
        return 0

    # Split mode validation
    if not args.input_files:
        parser.error("At least one input GeoJSON file is required for split mode")
    for fp in args.input_files:
        if not os.path.isfile(fp):
            sys.exit(f"ERROR: input file not found: {fp}")

    os.makedirs(args.output_dir, exist_ok=True)
    name, crs = extract_metadata(args.input_files[0])
    
    total_size_mb = sum(os.path.getsize(f) for f in args.input_files) / (1024 * 1024)
    print(f"Input  : {len(args.input_files)} file(s) ({total_size_mb:.2f} MB total)")
    print(f"Output : {args.output_dir}/<prefix>/<glac_id>.geojson")
    print("Streaming features & deduplicating on (glac_id, src_date)...")
    t0 = time.perf_counter()

    glaciers: Dict[str, List[dict]] = defaultdict(list)
    seen_keys: Set[Tuple[str, Any]] = set()
    total_features = 0
    duplicates = 0
    skipped_no_id = 0

    for input_path in args.input_files:
        print(f"  Streaming: {os.path.basename(input_path)}")
        for feat in iter_features(input_path):
            total_features += 1
            props = feat.get("properties") or {}
            glac_id = props.get("glac_id")
            src_date = props.get("src_date")
            
            if not glac_id:
                skipped_no_id += 1
                continue
                
            dedup_key = (glac_id, src_date)
            if dedup_key in seen_keys:
                duplicates += 1
                continue
                
            seen_keys.add(dedup_key)
            glaciers[glac_id].append(feat)
            
            if total_features % 10000 == 0:
                print(f"    Read {total_features:,} features | {len(glaciers):,} unique glaciers | {duplicates:,} duplicates so far...")

    print(f"\nGrouping complete. Found {len(glaciers):,} unique glaciers.")
    if skipped_no_id: print(f"  Skipped {skipped_no_id:,} features without 'glac_id'.")
    print(f"  Deduplicated   : {duplicates:,} features removed")

    # Write out & collect manifest data
    print("\nWriting glacier files...")
    manifest_data = {}
    written = 0
    skipped_single = 0
    
    for glac_id, feats in glaciers.items():
        if args.skip_single and len(feats) < 2:
            skipped_single += 1
            continue
            
        write_glacier_file(args.output_dir, glac_id, feats, name, crs, args.pretty)
        manifest_data[glac_id] = _build_manifest_entry(glac_id, feats)
        written += 1
        if written % 1000 == 0:
            print(f"  Written {written:,} files...")

    # Generate manifest
    if not args.no_manifest:
        m_path = write_manifest(args.output_dir, manifest_data, args.manifest_name)
        print(f"\n📦 Manifest written: {m_path}")

    elapsed = time.perf_counter() - t0
    out_size_mb = sum(
        os.path.getsize(os.path.join(root, f)) 
        for root, _, files in os.walk(args.output_dir) 
        for f in files
    ) / (1024 * 1024)
    
    print(f"\n✅ Done in {elapsed:.2f}s")
    print(f"  Input features  : {total_features:,}")
    print(f"  Glaciers written: {written:,}")
    if skipped_single: print(f"  Skipped (1 obs)   : {skipped_single:,}")
    print(f"  Output size     : {out_size_mb:.2f} MB")
    print(f"  Directory       : {args.output_dir}")

if __name__ == "__main__":
    sys.exit(main())