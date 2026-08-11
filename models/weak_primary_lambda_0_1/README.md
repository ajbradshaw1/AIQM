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

Weights are ordinary Git blobs so lab workstations do not depend on a separate
LFS endpoint. The GUI verifies all manifest hashes before inference. The
active release is `weak_shadow_only`, has four outputs and no 1x1 gate, is not
actionable, and does not drive automatic capture or replace the deployed
classifier.
