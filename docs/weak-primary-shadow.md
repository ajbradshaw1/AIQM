# Brightness-Robust Four-Output Shadow

The optional **Weak primary shadow** checkbox runs the 36-head
`four_output_no_1x1_all_extreme` ensemble. It is packaged under
`models/weak_primary_lambda_0_1/RHEEDClassify/brightness_robust_four_output_all_extreme/`
with its original manifest, frozen DINOv2 encoder, and all conditional heads.

The loader verifies the manifest contract, SHA-256 and byte count of every
artifact before loading. It requires `brightness_policy=all_extreme`,
`lambda_pair=0.1`, `execution_scope=weak_shadow_only`, and
`deployment_eligible=false`. An incomplete, modified, five-output, or
deployable package fails closed.

The shadow readout contains only Twinned (2x1), c(6x2), RT13, and HTR. It has
no 1x1 column. These are conditional primary-type probabilities, not surface
fractions, and remain non-actionable. The deployed GUI classifier and the
Equalizer's physical 1x1 basis are unchanged. This shadow output is not wired
to automatic capture.

`AI_REPO_ROOT` can override the bundled runtime checkout.
`AIQM_WEAK_PRIMARY_SHADOW_ROOT` can point directly to another compatible
four-output release. Device selection is automatic; use
`AIQM_WEAK_PRIMARY_DEVICE=cpu` or `cuda` to override it. Loading requires at
least 1536 MB of available physical memory unless a measured workstation limit
is configured with `AIQM_WEAK_PRIMARY_MIN_AVAILABLE_MB`.

## Workstation acceptance

Run this isolated CPU test before enabling the checkbox on ChMBE:

```powershell
python scripts/weak_primary_shadow_benchmark.py --device cpu `
  --iterations 60 --output logs/weak-primary-benchmark.json
```

The default gate requires load time <=120 s, p95 inference <=1 s, peak working
set <=2500 MB, steady RSS growth <=128 MB, four probabilities summing to one,
and `actionable=false`. Do not enable the shadow option unless the report says
`PASS`.
