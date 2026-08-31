# Copilot data-capture — actionable items

**Status:** proposal, not yet scoped or scheduled. Arising from the
2026-08-18 joint meeting and the 2026-08-19 physics/labeling review.
Owner for GUI implementation TBD — AJ to coordinate with Yao.

**Framing:** the ultimate reward is T_c of monolayer FeSe grown on the
STO. Everything below is judged by whether it moves us toward a dataset
that can train (a) the reconstruction classifier and (b) a growth
copilot that suggests next actions from growth history.

---

## Ranked items

| # | Item | Type | Serves | Blocked by |
|---|---|---|---|---|
| 1 | "Would you grow FeSe on this surface?" binary | Expert label | Copilot + reward | Nothing |
| 2 | History features + plateau detection | Automatic | Both models | Nothing |
| 3 | Action rationale on setpoint change | Expert, 1 click | Copilot | Small GUI change |
| 4 | Substrate heater setpoint channel | Automatic | Copilot | Small |
| 5 | Multi-label binary reconstruction presence | Expert label | Classifier | Labeling UI change |
| 6 | Retroactive outcome annotation of archived growths | Expert, ~1 hr | Reward | Grower time |
| 7 | 4-fold vs 6-fold symmetry check during FeSe growth | Automatic | Level-2 outcome | FeSe stage instrumented |
| 8 | Labeler expertise tier in schema | Schema | Label quality | Nothing |

Items 1, 2, 6, 8 need no new hardware, no CS-team dependency, and no
working end-to-end STO -> FeSe -> ARPES pipeline.

---

## 1. Terminal-judgment label

One binary per growth (and optionally per plateau): **"Would you grow
FeSe on this surface right now?"**

Rationale: it is binary (CS PI's preference), genuinely expert-only (MBE
PI's concern), directly proxies the true objective, and requires no
ARPES. Growers already make this call every growth — it is simply never
recorded.

Partially recoverable retroactively: the terminal state of a completed
growth is an implicit yes; an aborted/restarted growth is an implicit no.

## 2. History features (path dependence)

STO reconstruction is a *memory of the anneal*, not a function of
instantaneous temperature. Two frames at 300 C mean different things at
the start vs. on the ramp down. All ~130 sensor columns are
instantaneous; none are cumulative. This is the core data gap.

Derived per frame, **expanding-window / prefix-only** (see caveat below):

- `max_temp_reached_C` — single highest-value feature
- `dT_dt_C_per_s` — signed, smoothed; separates ramp-up/hold/ramp-down
- `time_above_800C_s`, `time_above_1000C_s`
- `thermal_budget` — Arrhenius-weighted integral of exp(-Ea/kT)dt at a
  few plausible Ea, since reconstruction formation is activated
- `growth_phase` — enum from dT/dt sign + event history
- `elapsed_since_phase_start_s`

Post-processing on existing sessions. Touches no live-growth code path.

## 3-4. Action logging

`set_change_events.csv` already exists and fires on operator setpoint
changes, but only records `channel="voltage"` and `channel="current"`
(`gui/growth_app.py:2984,3002`) — the MISTRAL PSU, not the substrate
heater. The substrate heater setpoint is the action that determines the
reconstruction, and the Eurotherm exposes it over Modbus TCP.

Add a `reason` field populated from a short fixed list at click time:

> surface not yet clean / waiting for pattern to sharpen /
> reconstruction achieved, moving on / overshot, backing off /
> responding to pressure or flux event / following standard recipe /
> other

Rationale converts an action log into supervised policy data and lets the
copilot explain itself.

**Retroactive:** the risers and treads in archived T(t) traces *are* the
action sequence. Changepoint detection recovers
`(timestamp, old_setpoint, new_setpoint, hold_duration)` for every
archived growth, including pre-GUI ones.

## 5. Binary reconstruction labels

The Equalizer mixture game is shelved. Replacement should be
**multi-label binary presence** (is 1x1 present? is c(6x2) present? ...)
rather than single-label forced choice — binary as the CS PI wants, no
numerical judgment as the MBE PI wants, but coexistence is still
expressible, which single-label would discard.

Keep an explicit out-of-set escape hatch (unknown / artifact / other)
for the ~1% of growths outside the five observed reconstructions. A
closed 5-way softmax cannot express "none of these".

## 7. Level-2 outcome — free, in situ

Wrong STO surface produces *hexagonal* FeSe, which is not
superconducting at all. Four-fold vs six-fold symmetry is directly
visible in RHEED. If the GUI is pointed at the FeSe deposition step,
this is a free binary outcome measurement with no ARPES needed.

## 8. Labeler provenance

`recon_labeler`, `recon_confidence`, `human_labeler`, `human_confidence`
already exist. Add an expertise tier (expert / trained / novice) so
non-expert labels can still be collected for coverage and agreement
measurement, then filtered or down-weighted downstream. Disagreement
between tiers flags visually ambiguous frames — the informative ones to
spend expert time on.

---

## Reward ladder

T_c requires STO growth -> FeSe growth -> ARPES, a workflow never yet run
end to end. Do not block on it; build the ladder:

| Level | Signal | Available | Latency |
|---|---|---|---|
| 0 | "Would you grow FeSe on this?" | Today | seconds |
| 1 | Target reconstruction achieved | Today | seconds |
| 2 | FeSe grew tetragonal (RHEED 4-fold vs 6-fold) | When FeSe grown | hours |
| 3 | T_c from ARPES | Rarely | days-weeks |

Standard multi-fidelity structure. Each level trains what it can now;
Level 3 arrives later and recalibrates the ones below.

---

## Open risks

**Attribution gap.** T_c depends on STO growth, FeSe deposition, *and*
post-growth anneal. We instrument only the first. Even once ARPES
numbers arrive we will not be able to tell whether a low T_c was a bad
STO surface or a bad FeSe deposition. Instrumenting the FeSe stage is
worth more than further refining STO instrumentation. Per the
literature, the post-growth vacuum anneal is where electron doping
actually happens.

**Pyrometer floor.** Archived traces sit flat at ~165 C at both ends —
that is the detector floor, not a temperature. Any history feature
computed in that regime measures the instrument. The 300 C example sits
only ~135 C above it; confirm with growers whether readings that low are
trustworthy.

**Pyrometer offset.** `docs/literature_comparison.md` reconstruction
conditions disagree with published temperature and atmosphere by
300-400 C. Most likely an emissivity/transparency offset (STO is
IR-transparent). Reconstructions could serve as a transfer standard that
is more trustworthy than the pyrometer itself.

**Substrate rotation.** Not recorded anywhere in the codebase. Heartbeat
default is 5.0 s; if the rotation period is a near-integer divisor, every
heartbeat frame samples the same azimuth.

**Prefix-only features.** At inference only the growth prefix exists, so
every history feature must be computable from the prefix. Whole-trajectory
statistics (total duration, final temperature) leak the future and will
silently inflate offline metrics.
