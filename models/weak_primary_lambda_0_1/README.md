# Weak-primary shadow runtime

This self-contained GUI runtime includes the minimal model definitions and two
audited artifact snapshots. The active GUI bundle is
`RHEEDClassify/brightness_robust_four_output_all_extreme/`, copied from the
2026-08-06 `four_output_no_1x1_all_extreme` release. Its original
`MANIFEST.json`, frozen DINOv2 encoder, and all 36 conditional heads are kept
unchanged. The shorter runtime path avoids Windows path-length failures.

The older `weak_primary_four_class_20260803` files remain only as provenance
for the preceding integration and are no longer selected by default. Shared
source files are byte-identical to the research checkout model definitions.

## Weights are NOT in Git

`*.pth` files are distributed separately and fetched on demand. They were
ordinary Git blobs until 2026-08-10 — 203 MB across 74 files, including the
82.6 MB DINOv2 encoder committed **twice**, byte-identical. Git history is
append-only, so every clone carried all of it forever.

```bash
python scripts/fetch_model_weights.py --source <dir>   # deployed set, 37 files
python scripts/fetch_model_weights.py --verify-only    # check what is present
```

`models/WEIGHTS_MANIFEST.json` records every path, sha256 and size. The
fetch script verifies each digest **before** a file is written, so a
truncated transfer or a wrong-model mix-up fails loudly rather than
producing quietly wrong classifications.

Two roles. `deployed` (37 files, 101 MB) is the active bundle the GUI loads
and is all a lab workstation needs. `benchmark` (37 files, 101 MB) is the
`weak_primary_four_class_20260803` provenance set described above, which is
no longer selected by default — fetch it only to reproduce the published
numbers.

The GUI still verifies all manifest hashes before inference. The
active release is `weak_shadow_only`, has four outputs and no 1x1 gate, is not
actionable, and does not drive automatic capture or replace the deployed
classifier.
