#!/usr/bin/env python3
"""Shape-parity check: deployment/spec-*.yml (prod) vs deployment/staging/spec-*.yml.

Staging must be exactly prod-shaped: same spec files, and per file the same
structure — mapping keys, list lengths, nesting. Scalar leaf VALUES are exempt
(hostnames, domain IDs, RPC URLs legitimately differ). Exits non-zero listing
every divergence, so CI catches "staging grew a key prod doesn't have" (and
vice versa) before any promotion does.

Run from the repo root:  python3 ops/scripts/check-spec-parity.py
"""
import glob
import os
import sys

import yaml

PROD_DIR = "deployment"
STAGING_DIR = "deployment/staging"
LEAF = "<value>"


def spec_names(d):
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "spec-*.yml")))


def skeleton(node):
    if isinstance(node, dict):
        return {k: skeleton(v) for k, v in node.items()}
    if isinstance(node, list):
        return [skeleton(v) for v in node]
    return LEAF


def diff(a, b, path, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: only in staging")
            elif k not in b:
                out.append(f"{path}.{k}: only in prod")
            else:
                diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: list length {len(a)} (prod) != {len(b)} (staging)")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out)
    elif type(a) is not type(b):
        out.append(f"{path}: {type(a).__name__} (prod) != {type(b).__name__} (staging)")


def main():
    prod, staging = spec_names(PROD_DIR), spec_names(STAGING_DIR)
    problems = [f"{n}: missing from {STAGING_DIR}/" for n in prod if n not in staging]
    problems += [f"{n}: missing from {PROD_DIR}/" for n in staging if n not in prod]
    for name in sorted(set(prod) & set(staging)):
        with open(os.path.join(PROD_DIR, name)) as f:
            p = yaml.safe_load(f)
        with open(os.path.join(STAGING_DIR, name)) as f:
            s = yaml.safe_load(f)
        diff(skeleton(p), skeleton(s), name, problems)
    if problems:
        print("Spec shape parity FAILED (prod vs staging):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"Spec shape parity OK: {len(prod)} specs match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
