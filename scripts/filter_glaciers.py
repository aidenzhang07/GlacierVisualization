#!/usr/bin/env python3
"""
filter_glaciers.py
------------------
Filters a GeoJSON FeatureCollection by property conditions, and can
query/count features matching arbitrary property expressions.
Both filter and query modes support --stream for constant-memory
processing of very large files.

MODES
-----
filter  (default)  Write a filtered GeoJSON file
query              Count / inspect features matching a condition (no output file)

FILTER OPTIONS
--------------
--field       Property field for null-check      (default: glac_name)
--null-str    String value treated as null        (default: "None")
--keep-null   Keep only features where field IS null/None
--min-area    Discard features with area < threshold
--max-area    Discard features with area > threshold
--area-field  Property holding the area value    (default: area)
--stream      Streaming parser — constant memory for huge files
--pretty      Pretty-print output JSON
--no-stats    Suppress summary output

QUERY & COMPOUND FILTER OPTIONS
-------------------------------
--query  EXPR  Expression to count/filter features.
               Supported operators: =, !=, <, <=, >, >=, contains, startswith, endswith
               Combine with AND (&&) or OR (||) for compound logic.
               
               TYPE INFERENCE RULES:
               • Unquoted values are treated as NUMBERS (e.g., area>5, db_area>=10.0)
               • Single/Double quoted values are treated as STRINGS (e.g., name='Athabasca', status="active")
               
               Examples:
               "db_area>=10.0 && glac_name='None'"
               "area!=0.0 && area<0.1"
               "glac_name contains Glacier || status='active'"
--query-show N  Print the first N matching feature properties (default: 10)

EXAMPLES
--------
# Filter using a compound expression (writes to named_filtered.geojson)
python filter_glaciers.py named.geojson --query "area>5 AND glac_name contains River"

# Stream filter on a huge file with OR logic
python filter_glaciers.py huge.geojson out.geojson --stream --query "glac_name='None' || area>=10"

# Count only (query mode)
python filter_glaciers.py named.geojson --query "db_area>=10.0 && glac_name='None'" --stream
"""
import argparse
import json
import os
import re
import sys
import time
from typing import Any, Callable, Generator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_null_value(value: Any, null_str: str) -> bool:
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
# Shared streaming core
# ---------------------------------------------------------------------------
def _find_features_bounds(path: str) -> tuple[int, int, str, str]:
    """Scan for the '[' and ']' wrapping the 'features' array."""
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
        sys.exit("ERROR: Could not find a 'features' key in the file.")

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
                if escape:
                    escape = False; continue
                if ch == "\\" and in_string:
                    escape = True; continue
                if ch == '"':
                    in_string = not in_string; continue
                if in_string: continue
                if bracket_byte == -1:
                    if ch == "[":
                        bracket_byte = abs_pos; depth = 1; continue
                if ch in ("[", "{"): depth += 1
                elif ch in ("]", "}"):
                    depth -= 1
                    if depth == 0:
                        close_byte = abs_pos; break
            if close_byte != -1: break
            byte_offset += len(chunk)
    if bracket_byte == -1 or close_byte == -1:
        sys.exit("ERROR: Could not locate the features array bounds.")
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
                # Outside strings
                if depth == 0:
                    if ch == '{':
                        depth = 1
                        buf = [ch]
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
                                    print(f"  WARNING: skipping unparseable feature: {e}", file=sys.stderr)
            else: continue
            break

# ---------------------------------------------------------------------------
# Query engine
# ---------------------------------------------------------------------------
_PRED_RE = re.compile(
    r"^(.+?)\s*(<=|>=|!=|<|>|=|contains|startswith|endswith)\s*(.+)$",
    re.IGNORECASE,
)
_SPLIT_RE = re.compile(r"(\|\||&&|\bOR\b|\bAND\b)", re.IGNORECASE)

def _parse_predicate(expr: str) -> tuple[str, str, Any]:
    """Parse 'field op value'. Infers type from quotes."""
    m = _PRED_RE.match(expr.strip())
    if not m:
        raise ValueError(
            f"Cannot parse predicate: {expr!r}\n"
            "Expected: <field><op><value>  e.g.  area>5  or  glac_name='Athabasca'\n"
            "Tip: Quote string values ('None', 'Active') for strict string matching."
        )
    field = m.group(1).strip()
    op = m.group(2).lower()
    raw = m.group(3).strip()

    # Type inference
    if (raw.startswith("'") and raw.endswith("'")) or \
       (raw.startswith('"') and raw.endswith('"')):
        value = raw[1:-1]  # String
    else:
        try:
            value = float(raw)
            if value.is_integer():
                value = int(value)
        except ValueError:
            value = raw  # Fallback to string
    return field, op, value

def parse_query(expr: str) -> list:
    """Parse compound expression into [pred, conn, pred, ...]"""
    tokens = [t.strip() for t in _SPLIT_RE.split(expr.strip()) if t.strip()]
    result = []
    for i, token in enumerate(tokens):
        if i % 2 == 0:
            result.append(_parse_predicate(token))
        else:
            upper = token.upper()
            if upper in ("&&", "AND"): result.append("AND")
            elif upper in ("||", "OR"): result.append("OR")
            else: raise ValueError(f"Unknown connector {token!r}. Use && / AND or || / OR.")
    if not result:
        raise ValueError("Empty query expression.")
    return result

def _eval_predicate(properties: dict, field: str, op: str, raw: Any) -> bool:
    """Evaluate predicate with strict type handling."""
    val = properties.get(field)
    _null_words = {"none", "null", ""}

    # String operators
    if op in ("contains", "startswith", "endswith"):
        s = "" if val is None else str(val)
        r = str(raw)
        if op == "contains":   return r.lower() in s.lower()
        if op == "startswith": return s.lower().startswith(r.lower())
        if op == "endswith":   return s.lower().endswith(r.lower())

    # Equality
    if op == "=":
        if isinstance(raw, (int, float)) and isinstance(val, (int, float)):
            return val == raw
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in _null_words):
            return val is None or (isinstance(val, str) and val.strip().lower() in _null_words)
        return str(val) == str(raw)

    if op == "!=":
        if isinstance(raw, (int, float)) and isinstance(val, (int, float)):
            return val != raw
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in _null_words):
            return not (val is None or (isinstance(val, str) and val.strip().lower() in _null_words))
        return str(val) != str(raw)

    # Numeric comparisons
    if op in ("<", "<=", ">", ">="):
        if val is None: return False
        try: nv = float(val)
        except (TypeError, ValueError): return False

        if isinstance(raw, (int, float)):
            nr = raw
        else:
            try: nr = float(raw)
            except (TypeError, ValueError): return False

        if op == "<":  return nv < nr
        if op == "<=": return nv <= nr
        if op == ">":  return nv > nr
        if op == ">=": return nv >= nr
    return False

def evaluate_compound(properties: dict, predicates: list) -> bool:
    """Left-to-right compound evaluation."""
    result = _eval_predicate(properties, *predicates[0])
    i = 1
    while i < len(predicates):
        connector, next_pred = predicates[i], predicates[i+1]
        if connector == "AND":
            if result: result = _eval_predicate(properties, *next_pred)
        else:
            if not result: result = _eval_predicate(properties, *next_pred)
        i += 2
    return result

def _query_label(predicates: list) -> str:
    """Readable reconstruction preserving type quotes."""
    parts = []
    for item in predicates:
        if isinstance(item, tuple):
            f, o, v = item
            if isinstance(v, (int, float)):
                parts.append(f"{f} {o} {v}")
            else:
                parts.append(f"{f} {o} {v!r}")
        else:
            parts.append(item)
    return "  ".join(parts)

def make_query_filter(predicates: list) -> Callable[[dict], bool]:
    def _keep(feature: dict) -> bool:
        props = feature.get("properties") or {}
        return evaluate_compound(props, predicates)
    return _keep

def run_query(input_path: str, predicates: list, show_n: int, stream: bool) -> None:
    t0 = time.perf_counter()
    total = 0; count = 0; shown: list[dict] = []
    source = iter_features(input_path) if stream else (
        iter(json.load(open(input_path)).get("features", []))
    )
    for feat in source:
        total += 1
        if evaluate_compound(feat.get("properties") or {}, predicates):
            count += 1
            if show_n and len(shown) < show_n: shown.append(feat.get("properties", {}))
    elapsed = time.perf_counter() - t0
    pct = (count / total * 100) if total else 0.0
    print(f"Query  : {_query_label(predicates)}  [{'streaming' if stream else 'in-memory'}]")
    if shown:
        print(f"\nShowing {len(shown)} of {count:,} match(es):")
        for i, p in enumerate(shown, 1): print(f"  [{i}] {json.dumps(p, ensure_ascii=False)}")
    print(f"\nScanned in {elapsed:.2f}s\n  Total features : {total:,}\n  Matching       : {count:,}  ({pct:.1f}%)")

# ---------------------------------------------------------------------------
# Filter predicate builder (Legacy)
# ---------------------------------------------------------------------------
def build_filter(field: str, null_str: str, keep_null: bool, min_area: float|None, max_area: float|None, area_field: str) -> Callable[[dict], bool]:
    _null_words = {"none", "null", ""}
    def _keep(feature: dict) -> bool:
        props = feature.get("properties") or {}
        val = props.get(field)
        null = val is None or (isinstance(val, str) and val.strip() == null_str)
        if keep_null:
            if not null: return False
        else:
            if null: return False
        if min_area is not None or max_area is not None:
            raw_area = props.get(area_field)
            if raw_area is None or (isinstance(raw_area, str) and raw_area.strip().lower() in _null_words): return False
            try: area = float(raw_area)
            except (TypeError, ValueError): return False
            if min_area is not None and area < min_area: return False
            if max_area is not None and area > max_area: return False
        return True
    return _keep

# ---------------------------------------------------------------------------
# Filter functions
# ---------------------------------------------------------------------------
def filter_standard(input_path, output_path, keep_fn, pretty):
    data = json.load(open(input_path))
    if data.get("type") != "FeatureCollection": sys.exit("ERROR: not a FeatureCollection.")
    feats = data.get("features", [])
    kept = [f for f in feats if keep_fn(f)]
    data["features"] = kept
    json.dump(data, open(output_path, "w"), indent=2 if pretty else None, ensure_ascii=False)
    return len(feats), len(kept)

def filter_streaming(input_path, output_path, keep_fn, pretty):
    _, _, header, footer = _find_features_bounds(input_path)
    total = kept = 0
    indent = "  " if pretty else ""
    sep = ",\n" + indent if pretty else ","
    with open(output_path, "w") as fout:
        fout.write(header)
        if pretty: fout.write("\n")
        first = True
        for feat in iter_features(input_path):
            total += 1
            if keep_fn(feat):
                if not first: fout.write(sep)
                fout.write(indent + json.dumps(feat, ensure_ascii=False) if pretty else json.dumps(feat, ensure_ascii=False))
                first = False; kept += 1
        if pretty: fout.write("\n")
        fout.write("]" + footer)
    return total, kept

# ---------------------------------------------------------------------------
# CLI & Main
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Filter or query GeoJSON by property conditions.", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("input", help="Input GeoJSON")
    p.add_argument("output", nargs="?", help="Output path (filter mode)")
    g = p.add_argument_group("Legacy Filters")
    g.add_argument("--field", default="glac_name")
    g.add_argument("--null-str", default="None")
    g.add_argument("--keep-null", action="store_true")
    g.add_argument("--min-area", type=float, default=None)
    g.add_argument("--max-area", type=float, default=None)
    g.add_argument("--area-field", default="area")
    q = p.add_argument_group("Compound Query")
    q.add_argument("--query", metavar="EXPR", help="Expression: 'field op value' combined with AND/OR. Quote strings, leave numbers unquoted.")
    q.add_argument("--query-show", type=int, default=10)
    o = p.add_argument_group("Options")
    o.add_argument("--stream", action="store_true")
    o.add_argument("--pretty", action="store_true")
    o.add_argument("--no-stats", action="store_true")
    return p.parse_args()

def main():
    args = parse_args()
    if not os.path.isfile(args.input): sys.exit(f"ERROR: {args.input} not found")
    size_mb = os.path.getsize(args.input) / (1024*1024)
    mode = "streaming" if args.stream else "in-memory"

    # Query Mode (no output specified)
    if args.query and not args.output:
        try: preds = parse_query(args.query)
        except ValueError as e: sys.exit(f"ERROR: {e}")
        print(f"Input  : {args.input} ({size_mb:.2f} MB)  [{mode}]")
        run_query(args.input, preds, args.query_show, args.stream)
        return 0

    # Filter Mode
    out = args.output or build_output_path(args.input)
    if args.query:
        try: preds = parse_query(args.query)
        except ValueError as e: sys.exit(f"ERROR: {e}")
        desc = _query_label(preds)
        keep_fn = make_query_filter(preds)
    else:
        active = []
        active.append(f"'{args.field}' IS null/\"{args.null_str}\"" if args.keep_null else f"'{args.field}' is NOT null/\"{args.null_str}\"")
        if args.min_area is not None: active.append(f"'{args.area_field}' >= {args.min_area}")
        if args.max_area is not None: active.append(f"'{args.area_field}' <= {args.max_area}")
        desc = " AND ".join(active)
        keep_fn = build_filter(args.field, args.null_str, args.keep_null, args.min_area, args.max_area, args.area_field)

    print(f"Input   : {args.input} ({size_mb:.2f} MB)")
    print(f"Output  : {out}")
    print(f"Filter  : {desc}")
    print(f"Mode    : {mode}\n")

    t0 = time.perf_counter()
    total, kept = (filter_streaming if args.stream else filter_standard)(args.input, out, keep_fn, args.pretty)
    elapsed = time.perf_counter() - t0
    if not args.no_stats:
        print(f"Done in {elapsed:.2f}s\n  Total features  : {total:,}\n  Kept            : {kept:,}\n  Removed         : {total-kept:,}")
    return 0

if __name__ == "__main__": sys.exit(main())