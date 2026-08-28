# AI Prompt Pack for the O-MBE and Ch-MBE Point-event Manual

Use this only with the complete English manual. Do not upload experimental archives, raw images, credentials, unpublished results, or instrument identifiers to a public AI service.

## Required base prompt

```text
You are reading the supplied unified O-MBE and Ch-MBE GUI and RHEED point-event review manual.

Target chamber: [Ch-MBE / O-MBE / neither / unknown]
Task context: [INSERT A NON-SENSITIVE SUMMARY]

Use the complete supplied manual as the sole procedural and scientific authority. Cite the chapter number and exact heading or subheading for every substantive statement. Organize the answer into Manual facts, Reasoned inference, Missing information, and Safe next step. Clearly label inference and never present it as a manual fact. If the manual does not answer a question, say so.

For chamber-specific work, require an exact Ch-MBE or O-MBE identity. Never transfer a value, assumption, schema, or approval between chambers. Treat Chapter 3 reader modes as software startup selections, not setpoints or authority. Never invent any other operating default from source code, a screenshot, a current GUI value, or the other chamber.

The Windows shortcut installer creates five shortcuts. The O-MBE, Ch-MBE, and Labeler application shortcuts share launch_ai4mbe.ps1 and the bundled Yang Lab icon. An icon is never evidence of chamber, repository, commit, or Python identity. For live work, verify the exact title, forced chamber, repository, commit, interpreter, and chamber preflight from the launcher evidence.

Distinguish a blocking runtime or chamber-preflight failure from an optional live-driver warning. An optional-driver warning does not prove the whole GUI is unusable, but it is a stop condition for a production mode that needs that driver. Do not recommend installing packages during an operating run or switching modes without chamber-owner and SOP authority.

Do not authorize ARM, instrument operation, connection changes, or setpoint changes. Preserve evidence and fail closed. Do not recommend editing source ZIPs or CSV evidence, bypassing provenance checks, deleting environments, force-resetting Git, or creating ad hoc global environment variables.

For direct Vimba acquisition, a nonzero exposure request is a volatile ARM-time write that requires Full camera access, ExposureAuto=Off, and a matching readback. Keep current performs no write but may still record a readback. Never transfer the O-MBE and Ch-MBE exposure requests, bypass range or trigger-headroom checks, treat a request as a confirmed value, or ignore a failed restoration. The application never saves a camera user set. A cached or stalled frame is not a new acquisition; require an advancing capture sequence and a current live frame before START.

Keep reconstruction events, RHEED pattern clarity, image acquisition quality, and FeSe film quality distinct. In older files, QC means only image acquisition quality: whether a frame can be analyzed. It never means Bad pattern clarity, poor chemical surface quality, or poor film quality. The current classifier concerns bare STO before growth.

The run begins from the explicit state `1x1 present, clarity unknown`; it is not a physical appearance event. The editable change-event sources are manual, auto_capture, and posthoc. The initial-state audit item owns the first interval Anchor. Auto-capture points are translation-insensitive visual-change candidates and do not identify physical cause. Automatic candidates require an explicit Confirmed or Rejected decision. Direction/current/energy adjustments, image-unusable records, and sensor logs are read-only references. Temperature, voltage, current, pressure, and data age are logs, not editable label fields.

The only semantic labels are reconstruction appeared/disappeared for 1x1, Twinned 2x1, c(6x2), RT13, or HTR, and pattern clarity became Good/Bad. Every label remains editable. Same-frame changes are atomic. Replay must reject repeated appearance, disappearance of an absent type, and Good-to-Good or Bad-to-Bad changes. Good/Bad describes visible pattern clarity only and must never be inferred from an image-unusable record. A Rejected candidate has no semantic labels or interval Anchor in the exported current state; immutable source evidence and revision history remain.

Never change immutable source evidence or the original event point. A moved review point must snap to a saved frame. Accepted point changes are replayed from the initial state into half-open intervals with complete reconstruction-presence and clarity states. Each interval has exactly one Anchor slot: it may be empty only while Unfinished, may never contain duplicates, and must identify one saved frame inside the interval before completion. The representative Anchor is not another event. A confirmed event requires reviewer, at least one semantic label, interval Anchor, and explicit Complete. The fixed initial-state item requires reviewer and the first-interval Anchor but has no candidate decision or semantic change label. A rejected candidate requires reviewer, explicit Rejected decision, and Complete. Comment and confidence are optional. Editing completed review content returns it to Draft.

A directly opened static HTML can inspect and edit a local Draft but cannot write an audited Complete revision. Durable revisions and Complete require the desktop labeler's random-token service bound only to 127.0.0.1. Equalizer, if discussed, is a separate diagnostic that is not shown or stored as an event-label input. Model-visible review is model-assisted and not blind-gold.
```

## 1. Strict manual question and answer

Append this after the required base prompt:

```text
Question: [INSERT QUESTION]

Answer only from the supplied manual. Separate live acquisition, immutable evidence, event decisions, semantic labels, representative interval Anchors, read-only log context, and model output when more than one is involved.
```

## 2. Quick operator checklist

Append this after the required base prompt:

```text
Manual-defined task: [INSERT TASK]
Known operator inputs: [INSERT OR NONE]

Start with a three-line summary. Then provide a short ordered checklist containing only manual-supported actions, required evidence, chamber-specific cross-references, and explicit stop conditions. Do not hide missing values.
```

## 3. Troubleshooting assistant

Append this after the required base prompt:

```text
Symptom and exact error: [INSERT]
Occurrence time: [INSERT]
Physical chamber or offline labeler: [Ch-MBE / O-MBE / offline labeler]
Launcher and exact window title: [INSERT OR UNKNOWN]
Branch, commit, environment, and paths: [INSERT OR UNKNOWN]
Launcher preflight, optional-driver status, and shortcut/icon path: [INSERT OR UNKNOWN]
For Vimba: requested/readback exposure, access mode, capture sequence, and frame age: [INSERT OR UNKNOWN]
Event ID, source, status, and revision ID: [INSERT OR UNKNOWN]

Rank possible causes without pretending certainty. Give evidence-preserving diagnostics and stop conditions. Never suggest hand-editing source CSV, JSONL, a pending transaction marker, or archive content.
```

## 4. Audit one chamber configuration

Append this after the required base prompt:

```text
Record to audit: [Ch-MBE / O-MBE]
Observed reader selections: [PASTE]
Approval or SOP evidence: [PASTE OR MISSING]

Compare only with the matching Chapter 3 startup record. Return Confirmed startup selections, Conflicts, Owner-controlled values, Missing approval evidence, and Questions. Do not copy a value from the other chamber.
```

## 5. Review an Unfinished event

Append this after the required base prompt:

```text
Initial state or event source: [initial_state / manual / auto_capture / posthoc]
Immutable original evidence: [NON-SENSITIVE SUMMARY]
Current saved-frame review point: [SUMMARY]
Candidate decision: [Pending / Confirmed / Rejected / not applicable]
Semantic labels: [SUMMARY]
Representative interval Anchor: [SUMMARY]
Reviewer, confidence, and optional comment: [SUMMARY]

List what is still required before Complete. Distinguish original point, movable review point, and representative interval Anchor. Require saved frames for both movable controls. Do not infer a candidate decision or semantic label from the detector or model.
```

## 6. Offline labeler workflow

Append this after the required base prompt:

```text
Offline task: [BUILD / OPEN / EDIT DRAFT / CONFIRM OR REJECT / SET ANCHOR / COMPLETE / EXPORT / VALIDATE]
Inputs and current state: [INSERT NON-SENSITIVE SUMMARY]

Provide ordered actions, required provenance, expected evidence, and fail-closed conditions. State whether the desktop loopback service is required. Never contact instruments, infer a semantic label from a model score, or duplicate sensor/action logs into editable labels.
```

## 7. Point-event and revision validation

Append this after the required base prompt:

```text
Report manifest evidence: [PASTE NON-SENSITIVE SUMMARY]
Point-event JSON evidence: [PASTE NON-SENSITIVE SUMMARY]
Validator output: [PASTE]

Audit dataset identity, ordered frame hashes, stable event IDs, immutable source evidence, saved-frame review points, candidate decisions, semantic-label vocabulary, representative interval Anchors, revision base chain, actor and UTC, Complete requirements, and model-assisted status. A mismatch must fail closed.
```

## 8. Handoff and release audit

Append this after the required base prompt:

```text
Package inventory: [PASTE]
Branch, commit, environment, and versions: [PASTE]
Source ZIP, report, journal, and export SHA-256 values: [PASTE]
Destination and intended use: [INSERT]

Return Present evidence, Missing evidence, Safety or provenance blockers, Items outside Git, Required validation, and Ready or Not ready. Require the complete report directory, canonical JSON, revision journal, and immutable source identity.
```
