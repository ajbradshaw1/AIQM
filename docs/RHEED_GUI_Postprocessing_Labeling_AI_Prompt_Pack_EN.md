# AI Prompt Pack for the O-MBE and Ch-MBE GUI and Offline Labeler Manual

- Prompt-pack version: **v1.6**
- Companion manual: [O-MBE and Ch-MBE GUI and RHEED Post-processing Labeling](RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.md)

## How to use this prompt pack

1. Supply the complete English manual to the AI. Do not provide selected pages when another chapter could affect the answer.
2. Copy the **Required base prompt** below and replace its bracketed fields.
3. Immediately append exactly one task prompt from Sections 1-8 and replace its bracketed fields.
4. Review the result against the manual and the applicable chamber SOP before relying on it.

Do not supply session archives, raw RHEED images, credentials, unpublished results, or other sensitive files to a public AI service. Chamber-owner entries are user-provided data to organize or audit; they are never operating authorization.

## Required base prompt

Every task prompt in this file is incomplete without this base prompt.

```text
You are reading the supplied unified O-MBE and Ch-MBE GUI and RHEED post-processing labeling manual.

Target chamber: [Ch-MBE / O-MBE / neither / unknown]
Task context: [INSERT A NON-SENSITIVE SUMMARY]

Use the complete supplied manual as the sole procedural and scientific authority. Cite the chapter number and exact heading or subheading for every substantive statement. Organize the answer into Manual facts, Reasoned inference, Missing information, and Safe next step. Clearly label inference and never present it as a manual fact. If the manual does not answer a question, say so.

For a chamber-specific task, the target chamber must be exactly Ch-MBE or O-MBE. If it is unknown, stop and request the chamber identity. Use only that chamber's launch route, startup-default record, troubleshooting route, and startup checklist. Never transfer a value, assumption, schema, or approval between chambers.

Treat the four reader-mode startup defaults in Chapter 3 as software selections, not setpoints or permission to ARM. Never invent any other operating default or infer it from source code, a current GUI selection, a demo screenshot, or the other chamber. Distinguish documented startup selection, owner-controlled operating value, and missing information.

Do not authorize ARM, instrument operation, a connection change, or any setpoint change. Only the applicable SOP and an authorized operator can do so. Preserve evidence and prefer fail-closed actions. Do not recommend bypassing validation, editing inputs to match predictions, deleting environments, force-resetting Git, or making ad hoc global environment changes.

Keep surface reconstruction, acquisition-quality QC, and FeSe film quality distinct. The current classifier concerns the bare STO surface before growth. Labels created while model outputs are visible are model-assisted and are not blind-gold labels. The offline labeler never controls instruments. Demo screenshots are not evidence of startup selections or operating values.
```

## 1. Strict manual question and answer

Append this after the required base prompt:

```text
Question: [INSERT QUESTION]

Answer only from the supplied manual. If the question crosses live acquisition and offline labeling, separate the two paths. Include the exact matching chamber cross-references when the question is chamber-specific.
```

## 2. Quick operator checklist

Append this after the required base prompt:

```text
Manual-defined task: [INSERT TASK]
Known operator inputs: [INSERT OR NONE]

Start with a three-line summary. Then provide a short ordered checklist containing only manual-supported actions, required evidence, matching chamber cross-references, and explicit stop conditions. Do not hide missing values inside the checklist.
```

## 3. Troubleshooting assistant

Append this after the required base prompt:

```text
Symptom and exact error: [INSERT]
Occurrence time: [INSERT]
Physical chamber or offline labeler: [Ch-MBE / O-MBE / offline labeler]
Launcher and exact window title: [INSERT OR UNKNOWN]
Displayed chamber identity: [INSERT OR UNKNOWN]
Branch and commit: [INSERT OR UNKNOWN]
Environment and selected paths: [INSERT OR UNKNOWN]
Relevant launcher-log summary: [INSERT OR UNKNOWN]

Rank possible causes without pretending certainty. Then give evidence-preserving diagnostic actions and stop or escalation conditions. For a live issue, route only through the matching chamber launch, default, troubleshooting, and checklist sections. For an offline issue, do not introduce live-instrument actions.
```

## 4. Audit one chamber configuration record

This single prompt applies to either chamber and replaces separate duplicated templates.

Append this after the required base prompt:

```text
Record to audit: [Ch-MBE / O-MBE]
Chamber-owner-provided entries: [PASTE ENTRIES]
Approver, revision, and date: [PASTE OR STATE MISSING]

Audit only the matching chamber record in Chapter 3, "Understand Configuration modes and chamber defaults." Treat the four reader modes as documented software startup selections, not as instructions or authorization. Return Confirmed startup selections, Conflicts or ambiguities, Owner-controlled values still missing, Missing approval evidence, and Questions for the named chamber owner. Do not silently normalize an unclear value. Reject or isolate any entry belonging to the other chamber. Do not declare an operating value approved without explicit approval evidence.
```

## 5. Compare proposed chamber records without transfer

Append this after the required base prompt. Set the base-prompt target chamber to `neither` because this is an audit of both records, not an operating task.

```text
Proposed Ch-MBE record: [PASTE]
Proposed O-MBE record: [PASTE]

Build a side-by-side audit table with separate Ch-MBE and O-MBE columns. For each item, label it Confirmed input, Conflict, Pending, Not applicable with approval, or Missing approval evidence. Treat matching proposed values as neither transfer permission nor proof that either value is approved. Identify cross-chamber contamination explicitly, but never suggest copying one value to resolve it.
```

## 6. Offline labeler workflow

Append this after the required base prompt. Set the base-prompt target chamber to `neither` unless the archived session identity is itself under audit.

```text
Offline task: [BUILD / OPEN / LABEL / EXPORT / IMPORT / VALIDATE]
Available inputs and paths: [INSERT NON-SENSITIVE PLACEHOLDERS]
Current message or state: [INSERT]

Begin with a brief route. Then provide ordered actions, required inputs, provenance checks, expected success evidence, and fail-closed stop conditions. Do not suggest editing inputs to bypass validation, copying only the HTML, or contacting instruments. Preserve the entire report directory and the canonical JSON according to the manual.
```

## 7. Annotation and provenance validation

Append this after the required base prompt. Set the base-prompt target chamber to `neither` unless chamber identity is part of the evidence being audited.

```text
Report or manifest evidence: [PASTE NON-SENSITIVE SUMMARY]
Annotation JSON evidence: [PASTE NON-SENSITIVE SUMMARY]
Validator output: [PASTE]

Check source-archive identity, ordered frames and hashes, model-context fingerprint, endpoint provenance, segment overlap, model-output visibility, and gold eligibility. Return Verified evidence, Validation blockers, Missing evidence, and Safe next action. A missing or mismatched binding must fail closed. Require model-visible annotations to retain eligible_for_gold=false.
```

## 8. Handoff and release audit

Append this after the required base prompt:

```text
Package inventory: [PASTE]
Branch, commit, environment, and versions: [PASTE]
Recorded SHA-256 values: [PASTE]
Destination and intended use: [INSERT]

Return a concise status summary followed by Present evidence, Missing evidence, Safety or provenance blockers, Items that must remain outside Git, Required validation, and a final Ready or Not ready result. Do not mark Ready when a required artifact, hash, validation result, version, chamber identity, or chamber-specific approval is missing. Require the complete report directory plus canonical JSON rather than HTML alone.
```
