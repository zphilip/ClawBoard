# artemis-control — Next-Step Development Plan

> Goal: make repeated (daily) phone tasks fast by reusing prior planner results and
> actions, while **not** adding heavy dependencies or duplicating the upstream
> Artemis committee verbatim.
>
> Principles: **reuse, don't recompute** · **lazy agents** (run only when
> evidence demands them) · **stdlib-only** (no langchain / langgraph / gemini-native)
> · **SafetyNet stays the deterministic verifier on every replayed step**.

---

## 0. Mental model: two different kinds of "memory"

This is the single most important clarification before implementation.

| | Upstream Artemis memory (session-scoped) | `mobile-control` memory (cross-run) |
|---|---|---|
| Scope | One run / one session | Survives across runs/days |
| Purpose | Context-window management + in-run retrieval | Reuse plans & actions, block bad ones |
| Persistence | Session dir (`data_engine.base_dir/`) | JSONL store on disk |
| What it remembers | Transcript, step summaries, notes | "this task → this plan → these actions + success/fail counters" |
| Reuses previous run? | **No** | **Yes** |

**Conclusion:** upstream Artemis has sophisticated *in-session* memory but
**no cross-run caching** — that is exactly why repeated tasks never get faster.
Porting `mobile-control`'s memory is therefore **complementary, not a
re-implementation** of Artemis's own memory.

---

## 1. Triage — what to port, emulate, or skip

| Upstream capability | Speed value | Complexity | Verdict |
|---|---|---|---|
| Cross-run plan/action memory (`mobile-control`) | 🔥🔥🔥 | Low | **Port now** |
| Persistent notes (`save/read/append note`) | 🔥🔥 | Trivial | **Port (make cross-run)** |
| Diagnoser | 🔥 (only stuck runs) | Low | **Port lazily** |
| Final Checker (`supervisor_provider`, already present) | 🔥 | Low | **Enable** |
| history_analyzer / log_analyzer / outputter | 0 | High | **Emulate** with deterministic `post_run_report` |
| Async LangGraph graph | ~0 (ADB is serial) | High | **Emulate** with one `threading` task |
| Gemini prompt cache / native runner | 0 (non-Gemini) | High | **Skip** |
| Token metering | 0 (observability) | Trivial | **Optional** counter |
| Trace replay telemetry / task-tree UI | 0 | High | **Skip** |
| Full MCP toolchain | 0 | Very high | **Skip** |
| object_detector / video_analyzer / image_processor | only if needed | High | **Skip for now** |

---

## 2. Phased rollout

### Phase 0 — Cross-run memory kernel (the core speedup)

Port into `artemis-control/memory/` (copy + adapt from
`ClawBoard/skills/mobile-control/memory/`):

- `signature.py` — intent signature + volatile-normalized UI fingerprint
- `models.py` — `StateSignature`, `ActionCandidate`, `MemoryRecord`, `PlanStep`, `TaskPlan`
- `store.py` / `plan_store.py` — JSONL persistence + health scoring
- `retriever.py` / `policy.py` — matching / decision policy
- `plan_executor.py` — ordered plan replay with per-step verification

**Wiring (in `artemis_agent.py`, both profiles):** insert a decision gate
**before** the Operator + Validator VLM calls, in priority order:

1. **plan-replay** — stored healthy plan for the intent → execute via ADB
   (skip Planner + Operator + Validator VLM)
2. **action-fastpath** — high-confidence cached action for current state
   (skip Operator + Validator VLM)
3. **VLM path unchanged** — cold start / fallback

**Verification:** every replayed step still passes `runner/safety_net.py`
(foreground-package, element signature, XML check). On failure → fall back to
VLM for that step only, mark the plan step `fail_count`, never self-reinforce.

Result: repeated daily task = **0..1 VLM calls** instead of `1+N` (flash) or
`1+2N+1` (pro).

**Status: ✅ implemented in `artemis-control-v2`** (2026-10-24).

- `memory/` — action-level kernel ported 1:1 from `mobile-control`
  (`signature.py`, `models.py`, `store.py`, `retriever.py`, `policy.py`,
  with Tier-1 ad-robust fuzzy matching) + artemis-native task-plan layer
  (`plan.py`).
- `runner/memory_runtime.py` — `MemoryRuntime` wires the kernel into both
  `_run_reactive_fallback` (flash) and `_run_plan_guided_fallback` (pro):
  plan-replay at run start, action-fastpath before the VLM decide, and
  recording of successful steps into the action store + plan store.
- `config.json(.example)` — new `memory` section:
  `enabled`, `mode` (`off|record|enforce`), `min_score`,
  `plan_min_successes`, `max_replays_per_state`, `store_dir`.
- Defaults to **`mode=record`** (safe observation; zero behaviour change) —
  set `mode=enforce` to activate replay, after a successful first run.
- Tap drift-safety: cached taps re-locate by stored `resource-id` against the
  current element tree; stale/lost elements fall back to the VLM.
- Replay cap: the same cached action is replayed at most
  `max_replays_per_state` (default 2) times per run, then the VLM takes over
  with full loop-detection notes.
- Tests: `tests/test_memory_runtime.py` (7/7 passing, run via
  `PYTHONPATH=. python3 tests/test_memory_runtime.py -v`).

**Known P0 simplifications (deliberate, revisit later):** pro fastpath skips
only the Operator VLM — Validator + SafetyNet still run per step; plan-replay
uses foreground-package verification per step instead of SafetyNet; `finish` /
`long_press` / `wait` are excluded from caching.

### Phase 0.5 — Memory matching robustness (ad / page-drift tolerant) ✅ implemented

**Problem (verified in `mobile-control`):** the memory kernel's hit gates were
*exact* — an `SHA-256` state key over the top-40 normalized UI-dump lines, and
exact resource-id/text equality. A single image-only ad banner (textless, so it
misses the keyword blacklist), a text ad using non-blacklisted wording
(`秒杀/限时/新品…`), a red badge (`99+`), or a feed card changes the hash →
state-match=0.5 → **cache miss**. Worse, the plan-replay `fail_count` increments
made ads *evict* healthy plans over time.

**Fix (Tier 1, deterministic, zero model cost):**

1. **Ad-container stripping by resource-id** — drop lines whose `id=` token is a
   known ad SDK (`ttad`, `ksad`, `pangle`, `admob`, `gromore`, `gdt`, `banner`,
   `splash`, `ad_*`, `*_banner`, …) even when the ad has no text.
2. **Component extraction** — `build_ui_components()` keeps
   `(foreground_pkg, resource_ids, labels)` instead of (only) an opaque hash.
3. **Fuzzy matching** — `ui_similarity()` scores screens 0..1 via weighted
   Jaccard overlap (`0.65·rid ∩ + 0.35·label ∩`), with foreground package as a
   hard gate. `retriever.score_record()` falls back to this when the exact
   `state_key` misses but the intent matches; `MemoryPolicy(min_score=0.7)` then
   accepts near-matches (e.g. 0.875 vs a single inserted ad card) while still
   rejecting a different app (≤0.5).

**Tier 2 (later, if hit-rate still insufficient):** replace pixel/string keys
with a **learned embedding** — DINOv2 (arXiv 2304.07193) or CLIP/SigLIP/Jina
CLIP v2 — matched by cosine similarity + ANN (faiss/nusac). **Tier 3:** lazy
GUI-grounding re-localization (OS-Atlas arXiv 2410.23218 / UI-TARS arXiv
2501.12326 / ShowUI / OmniParser arXiv 2408.00203) when Tier-1/2 drift exceeds
threshold. Keep the VLM fail-safe: never replay at low confidence.

**Status:** Tier 1 implemented + unit-tested in
`mobile-control` (`memory/signature.py`, `models.py`, `retriever.py`, `policy.py`,
wired in `run_gui_owl_1_5_for_mobile.py`; tests `tests/test_memory_fuzzy.py`,
9/9 passing). Carry the same code into the `artemis-control/memory/` port in
Phase 0.

### Phase 1 — Persistent notes (cross-run; ~40 lines)

Port `artemis/utils/notes.py` as `runner/notes.py` with
`save/get/append/update/list`. Keys: `task_plan`, `recovery_hints`, per-app
state.

**One deliberate change vs upstream:** store notes under a **persistent**
location (e.g. skill-local `memory/notes/` or `~/.clawberry/artemis-notes/`),
not the session dir. This is what turns upstream's per-session notes into
cross-run memory, and it raises the cache-hit rate by making states match
across days (e.g. "already logged in", "alarm list layout").

### Phase 2 — Lazy Diagnoser + enable Checker

- **Diagnoser** (`runner/diagnoser.py`): single text-LLM prompt, runs **only**
  when `consecutive_failures >= 2` (SafetyNet blocks) or Validator rejects
  twice. Output → `IncidentTracker.recovery_hints` + notes. Zero cost on
  healthy runs.
- **Final Checker**: enable `supervisor_provider` in `config.json` (set model).
  Runs **once per run**; skipped entirely when plan-replay completed with all
  milestones green (persisted `verified` flag).
- **Report** (`runner/post_run_report.py`): deterministic Markdown summary of
  `actions[]` + milestones + validator/safety stats. Replaces history/log
  analyzer agents (no LLM).

### Phase 3 — "Async" via one background thread

Move post-step work (visual summary, verification screenshot, notes flush)
off the hot path with stdlib `threading`. **Do not** pull in
langgraph/langchain/aiohttp — they multiply deps and startup latency.

### Deferred (documented as "not ported")

- Native Gemini runner / prompt cache — wrong provider; its benefit is already
  covered by Phase 0.
- Full MCP toolchain — skill talks to ADB directly; adds a process + socket for
  no speed gain.
- Trace replay telemetry / task-tree UI — post-mortem, not hot-path.

---

## 3. Target architecture

```
Instruction → intent sig + state fingerprint
   ├─ stored healthy plan? ──yes──▶ ADB replay step (SafetyNet verify)
   │                                  ├─ fail ──▶ Operator VLM
   ├─ high-conf cached action? ─yes─▶ replay action, verify
   │                                  └─ fail ──▶ Operator VLM
   └─ none → Operator VLM
        └─ 2+ consecutive failures? ──yes──▶ Lazy Diagnoser → notes/hints
        └─ end of run → optional Checker (skip if replay fully verified)
        └─ persist plan + notes (upsert)
```

---

## 4. Success metrics

- VLM calls / repeated run → 0 (plan hit), ≤1–2 otherwise
- Per-step latency (repeated task) → ~1–2 s (was 3–40 s)
- Cache hit rate & hit correctness
- Wrong-app incident rate
- New runtime deps → **0** (stdlib only)
- New module count → ~8 small files

## 5. Out of scope (explicitly)

LangChain/LangGraph runtime · Gemini prompt cache · MCP server ·
video/object/image agents · trace replay UI.

---

## 6. Gap report — status as of 2026-10-24

### 6.1 What changed since the previous review (already done)

- **Multi-provider config** — `config.json` now has a `providers` dict
  (`minimax` / `opencodego` / `guiowl`) with `api_key_env`, `opencode_headers`,
  and OpenAI-compatible header building (`_resolve_api_key`, `_build_openai_headers`).
- **Vision-First targeting** — `draw_indexed_dots()` annotates screenshots with
  element-index dots; the VLM addresses `target=N` instead of guessing pixels.
- **Stale-screen / loop detection** — `screenshot_diff_count()`,
  `similarity_note`, `targeted_indices`, bucketed-coordinate loop detection, and
  plan auto-complete when 0 `[ ]` milestones remain.
- **Per-step timing instrumentation** (`[timing]` logs).

Summary: coordination robustness improved substantially; the *speed-up* part
(memory) did **not** advance.

### 6.2 Gap vs the original Artemis project (still present)

| Upstream capability | artemis-control today | Status |
|---|---|---|
| Full plan grammar: CheckItem, finding lines, `[Loop]`/`[Loop:continuous]`, async planner validation | Now has `[Loop]` tags but **no CheckItem / finding lines / async validation** (`runner/planner.py` docstring) | ⚠️ partial |
| Real LangGraph runtime (graph, checkpoints, perception, state, visibility) | Single synchronous `while` loop | ❌ not ported |
| Persistent notes (`save/read/append/update note` → `notes/*.md`) | `scratchpad` dict merged in-RAM per turn; **never written to disk** | ❌ missing |
| `search_history` / `replay_steps` / `get_step_screenshot` as model tools | `HistoryStore.search()` exists but is internal-only | ⚠️ internal-only |
| Async visual step summarization (`StepMemoryService`) | `summary` field + `visual_summaries:true` exist, but **no code ever populates them** — `_prune_screenshots()` only sets `"[pruned]"` | ❌ stub |
| Committee agents (diagnoser, history_analyzer, log_analyzer, outputter, summarizer, object_detector, video_analyzer, image_processor, flash) | Only Planner(partial) + inline Operator + Validator + Checker + SafetyNet | ❌ missing |
| Gemini prompt cache / token metering / trace replay / MCP | none (correctly skipped for non-Gemini) | ⏭️ by design |

Model-lock re-confirmed: upstream is multi-provider (`artemis/llm/router.py` +
LangChain); our `providers` config already mirrors this. Gemini native runner
stays out of scope.

### 6.3 Gap vs this DEV_PLAN (phase-by-phase)

| Phase | Planned | Actual | Verdict |
|---|---|---|---|
| 0 — cross-run memory kernel | copy `mobile-control/memory/` + decision gate before VLM | `artemis-control/memory/` is **empty** | ❌ not started (biggest gap) |
| 0.5 — ad/drift-robust matching | carry Tier-1 fix into Phase-0 port | Done + tested in `mobile-control` (9/9); **not copied yet** | ⚠️ done upstream-of-port only |
| 1 — persistent notes | `runner/notes.py`, persistent path | `scratchpad` still in-RAM only | ❌ not started |
| 2a — lazy Diagnoser | `runner/diagnoser.py`, fire on 2+ consecutive failures | Not ported; only `IncidentTracker` hints | ❌ not started |
| 2b — enable Checker | set `supervisor_provider.model` | model is `""` → Checker falls back to VLM; flash never runs Checker | ⚠️ half-done |
| 3 — async via one thread | background summary / notes flush | No background thread; visual summaries are stubs | ❌ not started |

**Net:** DEV_PLAN is ~10–15% executed. All completed work went into
robustness/coordination; every *speed* item (Phases 0, 1, 3) is still open.

### 6.4 Flash vs Pro — where the fork really stands

| | Flash | Pro |
|---|---|---|
| Planning VLM | ✅ always | ✅ always |
| Element index + dots | ✅ | ✅ |
| Operator VLM / step | ✅ 1 | ✅ 1 |
| Validator VLM / step | ❌ | ✅ 2nd VLM |
| SafetyNet (deterministic) | ❌ | ✅ |
| Final Checker | ❌ never runs | ✅ (VLM fallback while supervisor disabled) |
| **VLM calls ≈** | **1 + N** | **1 + 2N + 1** |

Doc/behavior mismatches to fix:

1. `SKILL.md` says Flash = "reactive, no plan", but flash always runs
   `generate_plan` (planning VLM + screenshot) before step 1. Flash is actually
   "plan-guided minus safety net/validator/checker".
2. `config.json` has `context.budget_limit` but `config.json.example` omits it
   (silently falls back to 80000).
3. ⚠️ `config.json` contains a live MiniMax API key in plaintext — rotate and
   move to `api_key_env`.

---

## 7. Next-step improvement plan (phase by phase, revised)

Priority order follows *speed impact ÷ complexity* and reuses tests that already
pass in `mobile-control`.

### Phase 0 — Cross-run memory kernel (the core speedup; port now)

Copy `mobile-control/memory/` → `artemis-control/memory/`, **including the
Tier-1 fuzzy matching from Phase 0.5** (`signature.py`, `models.py`, `store.py`,
`retriever.py`, `policy.py`, `plan_store.py`, `plan_executor.py`).

Wire a decision gate **before** `generate_plan` and `_decide_action_plan_guided`:

1. plan-replay (healthy plan for canonical intent → ADB steps, Verified by
   existing `SafetyNet`),
2. action-fastpath (high-confidence cached action for current UI components),
3. VLM path unchanged.

**Adaptation specifics (artemis differences):** artemis uses pixel coords +
`target=N` element indices (not 0–1000 normalized coords); reuse
`ElementIndexer` for target resolution and `safety_net.py` as the deterministic
verifier. Keep the existing fail-safe (fall back to VLM, mark `fail_count`).
**Goal:** repeated daily task = 0..1 VLM calls.

### Phase 1 — Persistent notes (cross-run)

Port `runner/notes.py` (save/get/append/update/list) backed by a **persistent**
dir (`memory/notes/`). Route `scratchpad` keys (`task_plan`, `recovery_hints`,
per-app state) into it. Raises cache-hit rate by making cross-day states match.

### Phase 2 — Lazy Diagnoser + enable Checker

- `runner/diagnoser.py`: text-LLM, fires only on ≥2 consecutive SafetyNet
  blocks / Validator rejects → `IncidentTracker.recovery_hints` + notes.
- Set `supervisor_provider.model` (text LLM) so the final Checker uses it; skip
  Checker when plan-replay verified all milestones green.
- `runner/post_run_report.py`: deterministic Markdown summary (no LLM).

### Phase 3 — Background thread + working visual summaries

Make `visual_summaries` actually work: a stdlib `threading` background task that
replaces the post-action screenshot with a text summary into
`StepRecord.summary` (fixes the `"[pruned]"` stub). Flush notes off the hot path.

### Phase 4 — Fix doc/config drift (cheap, high trust)

1. Correct `SKILL.md` Flash description (plan-guided, no safety/validator/checker).
2. Add `context.budget_limit` to `config.json.example`.
3. Seeded prompt-cache affinity via `--cache-affinity` (already no-op-safe) — or
   drop the half-wired knob.

### Phase 5 — Tier 2/3 matching escalation (only if hit-rate still low)

- Tier 2: DINOv2 / CLIP / SigLIP screen embedding + cosine ANN as an alternative
  state key (lazy, on Tier-1 miss).
- Tier 3: lazy GUI-grounding relocalization (OS-Atlas / UI-TARS / ShowUI /
  OmniParser) when drift exceeds threshold.

### Deferred (still out of scope)

Native Gemini runner · MCP toolchain · trace-replay telemetry ·
object_detector / video_analyzer / image_processor agents.

### Success metrics to gate each phase

- VLM calls / repeated run → 0 (plan hit), ≤1–2 otherwise
- Per-step latency (repeated) → ~1–2 s (was 3–40 s)
- Cache hit rate + hit correctness (no wrong-app replay)
- Wrong-app / incident rate
- New runtime deps → 0 (stdlib only)

---

## 8. Gap report — status as of 2026-10-25 (post Phase-0, ready for Phase 1/2)

### 8.1 Where v2 stands now (updated vs §6)

Phase 0 is **done and verified on-device**: record + enforce + plan-replay
(0 VLM on replay), ad-robust Tier-1 matching, foreground-package hard gate with
element-majority fallback, VLM-failure fallback steps excluded from learning,
`memory/memstat.py` inspector, and the config passthrough fix. The §6 gaps
"memory/ empty" / "config dropped sections" are **closed**.

Remaining committee-agent gaps are now: **persistent notes**, **lazy
diagnoser**, **deterministic post-run report**, **async/visual summaries**,
and **real LangGraph runtime** (out of scope by design).

### 8.2 Detailed Phase-1 / Phase-2 gap (the focus now)

| Upstream Artemis | artemis-control-v2 today | Gap to close |
|---|---|---|
| `utils/notes.py` — `save/read/append/update/list` note tools (line-range reads, `task_plan` key) | `scratchpad` dict, in-RAM only, VLM `notes_updates` merged per turn, never persisted | **Phase 1**: port notes as `runner/notes.py`, persist under `memory/notes/` (cross-run, not session-scoped) |
| `agents/diagnoser/` — lazy agent w/ device tools (`analyze_logs`, `read_note`, `run_short_adb_command`, `wait`, `ui_hierarchy`), fires only when stuck, ends in `submit_answer` | only `IncidentTracker` hints (validator rejections / safety-net blocks); no diagnosis agent | **Phase 2a**: `runner/diagnoser.py` text-LLM, fire on ≥2 consecutive failures, write `recovery_hints` → notes |
| `agents/checker/` — zero-side-effect final audit, read-only probes, structured `CheckVerdict` (passed/failed/inconclusive) | `runner/checker.py` exists + wired in Pro; Supervisor `model=""` so it falls back to VLM; **flash never runs checker**; verdict is a loose dict, not the upstream check-item table | **Phase 2b**: enable `supervisor_provider.model`; skip checker on fully-verified replay; keep loose dict (check-item table only if we port plan grammar) |
| `agents/outputter/` + `history_analyzer` + `log_analyzer` — LLM post-mortem report | nothing; result JSON only | **Phase 2c**: `runner/post_run_report.py` deterministic Markdown (no LLM) — deliberately replaces the 3 LLM report agents |

### 8.3 Further improvement items remaining (backlog, ordered)

1. **Tier 1.5 — compound-key tap relocalization** ✅ **done (2026-10-25)**:
   when a recorded tap has no `resource_id`, relocate by
   `resource_id` → `(class_name + text)` → `content_desc` with a centre-distance
   tiebreak. Removes the last pixel-rigid path on replay.
2. **Phase 3 — async thread + real visual summaries**: `StepRecord.summary` is a
   `"[pruned]"` stub today; add a stdlib `threading` task to summarize the
   post-step screenshot and flush notes off the hot path.
3. **Checker skip-on-replay**: persist a `verified` flag with the stored plan so
   a fully-verified replay bypasses the final Checker.
4. **Supervisor/Checker correctness**: use the text LLM for the final verdict,
   and fail closed (→ not learned) when the checker disagrees with "finish".
5. **Plan-grammar completeness**: CheckItem / finding lines / async validation
   (upstream `plan_grammar.py`) — only if task complexity demands it.
6. **Doc/config drift**: `SKILL.md` flash description; `context.budget_limit` in
   `config.json.example`; rotate the plaintext MiniMax key (still present).
7. **Tier 2/3 matching escalation** (only if hit-rate still insufficient after
   1.5): CLIP/SigLIP screen embedding, then GUI-grounding relocalization
   (OS-Atlas / UI-TARS / OmniParser). Deferred on ARM SBC cost grounds.

### 8.4 Phase 1/2 build checklist (concrete)

- `runner/notes.py`: `save_note(key,text)/read_note(key,start,end)/append_note/
  update_note(target,repl)/list_notes()` over `memory/notes/*.md`; map
  `task_plan` → `task_plan.md` (mirror upstream `get_note_file_path`).
- Wire notes into the loop: flush `scratchpad` at run end + expose `recovery_hints`
  back into the next run's prompt (cross-run hints are the diagnosis win).
- `runner/diagnoser.py`: prompt + `call_text_llm`, input = last N incidents +
  notes + step descriptions; output = hints dict → `notes["recovery_hints"]`.
- `runner/post_run_report.py`: Markdown from `actions[]` + plan + validator/
  safety stats; emitted as a `report` result field.
- Config: `supervisor_provider.model` set (text LLM), `profiles.pro.verification=final`.

### 8.5 Implemented (2026-10-25 — Tier 1.5 + Phase 1 + Phase 2)

- **Tier 1.5 compound-key relocalization** — `ArtemisPlanStep` now stores
  `class_name` / `text` / `content_desc`; `find_element_by_compound` relocates by
  `resource_id` → `class_name + text` → `content_desc` (centre-distance tiebreak).
  Used by both plan-replay and action-fastpath; `record_action` captures the
  compound key + persists `_cls/_text/_cd` into the action store. Tests:
  `TestCompoundRelocalize`.
- **Phase 1 persistent notes** — `runner/notes.py` ports the upstream 5 ops
  (`save/read/append/update/list`, `task_plan` → `task_plan.md`) over
  `memory/notes/` (cross-run, gitignored). `MemoryRuntime` exposes
  `save_note/read_note/append_note/update_note/list_notes` and persists the
  in-run `scratchpad` as `_scratchpad.json` (capped at `scratchpad_max_keys`);
  both loops reload it at start and flush at end via `_finalize_run`.
- **Phase 2a lazy Diagnoser** — `runner/diagnoser.py`: text-LLM when a caller is
  given, deterministic fallback otherwise; fires once per failure burst when
  `incident_tracker.consecutive_failures >= diagnoser.consecutive_failures`
  (default 2). Wired into Pro via `_maybe_diagnose()` at validator-reject and
  safety-net-block sites; emits a `diagnosis` event, feeds `recovery_hints` back
  into the next Operator prompt, and saves `notes`.
- **Phase 2b enable Checker** — `_get_text_llm_caller()` now always returns the
  text caller (uses `supervisor_provider` when set, else primary provider
  text-only; `call_checker` still VLM-falls-back). `config.json.example`
  documents `supervisor_provider` for Checker + Diagnoser.
- **Phase 2c deterministic report** — `runner/post_run_report.py` builds
  Markdown post-mortem (goal / outcome / plan / actions / validator stats /
  checker verdict / timing) with no LLM, written under `memory/reports/`.
  Both loops call `_finalize_run` and attach `debug.post_run_report`.

Still open (unchanged): Phase 3 async+visual summaries; checker skip-on-replay;
plan-grammar completeness; doc/config drift; Tier 2/3 matching escalation.

---

## 9. Gap report — status as of 2026-11-01 (post probe_device + check-item port)

### 9.1 Closed since §8

- **`probe_device` (read-only enumerated probes)** — `runner/probes.py` mirrors
  upstream `agents/checker/checker.py`: `PROBES` table (`alarms/battery/
  foreground/notifications/audio/setting/content/packages/prop`), regex-validated
  `build_probe_argv` (shell injection structurally impossible), `derive_probes`
  (`[probe: …]` hints + keyword rules), `summarize_probe_output` (decisive-lines
  filter + truncation). Added `audio` probe (`dumpsys audio`) that upstream lacks
  as the canonical mute signal.
- **Check-item grammar** — `runner/planner.py` now parses `- verify:` /
  `- assert:` / `- verify@end:` into `CheckItem(kind, when, text, parent_key)`,
  anchors to milestones, and teaches the grammar + a `[probe: audio]` state-change
  example in `_PLAN_GRAMMAR_BASE`.
- **Checker threads check-items + probes** — `call_checker(..., check_items,
  probe_results)`; `artemis_agent.py` `_snapshot_check_items` + `_run_check_probes`
  + `_call_checker_with_probes` wired into the final Checker and all 4 arbitration
  sites. `_unjudged_check_items` backfills verdicts on LLM/parse failure.

### 9.2 Remaining gap vs upstream Checker architecture

The probe tool itself is ported, but the **verification architecture around it**
is still a simplified single-shot audit. These are `probe_device`-class gaps —
state/evidence mechanisms, not cosmetics:

| # | Upstream mechanism (`agents/checker/checker.py`, `utils/plan_grammar.py`) | v2 today | Impact |
|---|---|---|---|
| 1 | **Checkpoint audit** — `run_checkpoint_check` at each milestone completion; evidence **anchored** (anchor step pre-action shot + previous post state + persistent probes); checkpoint deliberately has **no live-screen access** | One `call_checker` at task exit; `on_complete` + `@end` collapse into a single audit against the **final** screenshot | Transient states (toasts, dialogs, ordering) can't be judged after the device moved on |
| 2 | **`verify` repair loop** — failed `on_complete` verify sends its milestone back for re-execution | Prompt/SKILL *say* this, but no re-execution exists; a failed verify only flips final status to `checker_failed` | The agent can't self-correct a missed state change |
| 3 | **Agentic Checker tool loop** — Checker iterates (`CHECKER_MAX_ITERATIONS`) and calls `search_history`/`replay_steps`/`get_step_screenshot`/`read_note`/`probe_device` itself | Single-shot LLM call; probes pre-run by `_run_check_probes` and passed as static text | Can't issue a follow-up probe when the first is indecisive (needs both `audio` **and** `setting global zen_mode`); can't query step history/screenshots |
| 4 | **Node-side verdict hygiene + fail-open release** — `verdicts_allow_release` (release computed outside the model), `_normalize_report` (evidence-less `failed` → `inconclusive`; every item gets a verdict), `unmet_subgoals` list | Outcome keys on model `goal_achieved`; vague `failed` **not** downgraded; no `unmet_subgoals` | A hallucinated failure wrongly sets `status=checker_failed` |
| 5 | **Finding-line projection** — `FINDING_LINE_RE` + `strip/apply_finding_lines` re-project unresolved verify findings into the plan (single-direction, idempotent) | Absent | Failed verify has no persistent "what's still wrong" line for the operator |
| 6 | **Check-item mutation guards** — `missing_check_items`/`check_items_changed`/`restore_missing_check_items` prevent plan rewrites from silently deleting/rewriting declared check lines | Absent | A plan regeneration silently drops the acceptance contract |
| 7 | **`disable_device_probes` config toggle** — upstream `probes_enabled` / `execution_setup.disable_device_probes` | Always probes when check items exist | Can't skip probes on devices lacking `dumpsys` privilege |
| 8 | **Conditional prompt segment assembly + assert semantics** — verify/assert/anchor/final/probe segments assembled conditionally; `assert` taught precondition-first (never `failed` for missing evidence; device-defect attribution) | Monolithic system prompt; `assert` ≈ `verify`; no precondition-in-history rule | Mis-judged `assert` as execution failure vs. device defect |

Minor (non-blocking): `PlanSnapshot` lacks `has_top_level`/`continuous_top_level`/
`first_active`/`last_active`/`parent_of`/`active_leaf`; `PlanItem.key` imports
`hashlib` inside the property (hot path); `probe_results` is keyed by check-item
**text**, so two identical texts collide.

### 9.3 Priority order for the next round (same logic as always: value ÷ complexity)

1. **Gap 1 + 2 (checkpoint audit + verify repair loop)** — this is the actual fix
   for "mute took several rounds to find the setting"; judging at completion with
   anchored evidence and repairing a failed milestone is the whole point of
   `on_complete`. Highest value; medium complexity (needs per-milestone hook +
   re-open-milestone logic).
2. **Gap 4 (verdict hygiene + fail-open release + `unmet_subgoals`)** — cheap,
   pure-function change; closes the hallucinated-failure false-negative.
3. **Gap 6 + 5 (mutation guards + finding lines)** — cheap grammar helpers;
   make the acceptance contract un-deletable and visible.
4. **Gap 3 (agentic Checker loop)** — biggest complexity, smaller marginal value
   given probes are pre-derived; do only if the pre-run probes prove insufficient.
5. **Gap 7 + 8 (config toggle + prompt segments/assert semantics)** — trivial
   hygiene, bundle with any touching of `checker.py`/`config.json`.

---

## 10. Implementation plans for gaps 1–4 (deep-dive, 2026-11-01)

Authority: `artemis/agents/checker/{checker.py,checker.json}`,
`artemis/graph/checkpoints.py`, `artemis/graph/graph.py`
(exit-settlement), `artemis/utils/plan_grammar.py`. All plans keep v2
stdlib-only + synchronous; "concurrent checkpoints" collapse to in-line calls
because ADB is serial anyway.

---

### Gap 1 — Checkpoint audit at milestone completion (anchored evidence, no live screen)

**Upstream mechanism (ground truth):**
- `queue_checkpoints()` runs at plan-write time. For every top-level subgoal that
  just flipped to `[x]` *and* carries `on_complete` check items, it enqueues a
  `PendingCheckpoint(checkpoint_id=subgoal_hash, subgoal_text, check_items,
  plan_text, trigger_ts)`.
- Spawning is **deferred** to after the turn's step is recorded, so the
  `EvidenceAnchor(anchor_step_id, trigger_ts, plan_text)` points at the right
  step (not the previous turn).
- `run_checkpoint_check` audits the milestone with the tool table + prompt
  **denying live-screen access** — evidence is the anchored history (anchor
  step's pre screenshot + preceding step's post state) + persistent probes.
  The `anchor_guide` prompt segment codifies: transient states MUST be read from
  history, never from the current screen.

**v2 adaptation:**

1. New `runner/checkpoints.py` (stdlib): `EvidenceAnchor`, `PendingCheckpoint`,
   `CheckpointRun` dataclasses; JSONL ledger `memory/check_ledger.jsonl`
   (`append_ledger_record`/`read_ledger` — append-only, mirror upstream);
   config accessors (`midway_checks_enabled`, `checkpoint_max_repairs`,
   `checkpoint_timeout`).
2. New `call_checkpoint_check(...)` in `runner/checker.py` (or a thin wrapper in
   `checkpoints.py`): same engine as `call_checker` but
   - `screenshot_path=""` (no live screen),
   - `check_items=` **only** the milestone's `on_complete` items
     (`snapshot.check_items_of(item)`),
   - `probe_results=_run_check_probes(on_complete items)`,
   - prompt gains the `anchor_guide` / `verify_semantics` segments (Gap 8).
3. Hook in the pro loop right after `apply_plan_updates(...)`:
   diff `parse_plan(before)` vs `parse_plan(after)`; for each top-level milestone
   that flipped `[/]→[x]` with `check_items_of(item)` non-empty, run
   `call_checkpoint_check` **inline** and append verdicts to the ledger.
   Anchor = current `step` count + the action description just executed (v2 has
   no per-step screenshot ledger; a `step_index` anchor is the cheap first cut).
4. Config: `checks.midway_enabled` (default `true` in `pro`, `false` in `flash`).

**Acceptance:** a `pro` run whose plan carries `- verify: 铃声已静音 [probe: audio]`
under the mute milestone fires one checkpoint audit at the moment that milestone
is marked `[x]`, with no live screenshot, judged by `dumpsys audio`.

---

### Gap 2 — `verify` repair loop (re-open failed milestones; bounded retries)

**Upstream mechanism:**
- `harvest_run` computes `verify_failures`; if any and the subgoal is still
  current and the run isn't user-stopped, and `repairs < checkpoint_max_repairs`
  (default **2**): `revert_subgoal_status()` flips `[x]→[/]`, registers a
  standing `- finding:` headline, writes the `checker-<id>` repair-log note, and
  injects `operator_feedback` findings (+ the failed verdict's `suggestion`).
  Quota exhausted → leave `[x]`, stop repairing.
- Final check (`exit_settlement_node`): `passed = verdicts_allow_release(report)
  and not report.unmet_subgoals`. If not passed and attempts <
  `final_check_max_attempts` (3) and no stop/halt latched: `revert_subgoal_status`
  every `unmet_subgoals` entry + inject findings, route **continue** (re-enter
  the loop). Else: BLOCKED outcome with the last findings.
- `resolve_item_status`: verify = **latest substantive verdict wins**
  (repaired-then-passed ⇒ passed); assert = first failure is permanent.

**v2 adaptation:**

1. `revert_subgoal_status(plan_text, subgoal_key) -> tuple[str,bool]` pure
   function in `runner/planner.py` (regex-flip top-level `[x]→[/]` whose
   `subgoal_hash` matches).
2. In the checkpoint hook (Gap 1): on any `failed` verify with non-empty
   evidence, and `repairs_used[milestone_key] < checkpoint_max_repairs`:
   - `plan_text = revert_subgoal_status(...)`,
   - append `[verify failed] <criterion>: <evidence> Suggestion: <suggestion>` to
     the next Operator prompt as recovery feedback,
   - record `repairs_used += 1` + ledger record.
   The existing `while` loop then re-runs the milestone.
3. In the final Checker path (`~artemis_agent.py:2238`): replace the raw
   `checker_result["goal_achieved"]` decision with `verdicts_allow_release` (Gap
   4); on unmet verify criteria with attempts `< final_check_max_attempts`:
   revert unmet subgoals + set `end_reason` so the loop continues; else
   `checker_failed`/`blocked`.
4. Fold `resolve_item_status` + a `TestSummary` (passed/failed/inconclusive/
   unchecked + failed_items) into `post_run_report.py` from the ledger, so a
   repaired-then-passed item reads `passed`.

**Acceptance:** when a checkpoint verify fails with evidence, the milestone is
set back to `[/]`, the finding + suggestion reach the operator, and the
milestone is re-attempted (max 2 repairs) before the run is allowed to settle.

---

### Gap 3 — Agentic Checker tool loop (probe/history tools the Checker drives)

**Upstream mechanism:**
- `_run_check_loop` iterates to `CHECKER_MAX_ITERATIONS`: `llm.bind_tools(tools)`;
  if a turn has no tool calls → emit the structured `CheckReport`; else execute
  each tool call and feed `ToolMessage`s back; final iteration forces a verdict;
  `_normalize_report` sanitises after the loop.
- `build_checker_tools(ctx, entry)` assembles a **read-only** table:
  `search_history`, `replay_steps`, `get_step_screenshot`, `read_note` +
  `probe_device` (registration is gated by `probes_enabled`).
- Structured output via pydantic `CheckVerdict`/`CheckReport`.

**v2 adaptation (stage-gated; stdlib only, no pydantic):**

*Stage 3a — bounded re-probe (do first, small):*
1. Extend `_run_check_probes` to run **every** derived probe candidate per item
   (union of `[probe:]` hints + keyword matches), not just the first — e.g. mute
   yields both `dumpsys audio` and `settings get global zen_mode` evidence.
2. Add an optional `"probe_more": [{"kind","params"},...]` field to the Checker
   JSON schema; after a first `call_checker`, if the response requests more
   probes, run them via `build_probe_argv` (still enumerated/safe) and re-invoke
   the checker once with the extra evidence. Cap at `checker_max_rounds` (2).

*Stage 3b — full loop (later, only if 3a insufficient):*
3. New `runner/checker_loop.py` mirroring `_run_check_loop`: loop with tool
   functions `probe_device` (enum via `build_probe_argv`), `read_note`
   (`runner/notes.py`), `search_history` (over `actions[]` + ledger),
   `get_step_screenshot` (from `screenshots_dir`). Tool calls are plain-JSON
   (not actual langchain tools); each returns one string; iterations capped;
   final turn forced to a verdict dict.
4. Reuse the ledger + `_normalize_checker_result` (Gap 4) as the structured
   `CheckReport` equivalent.

**Acceptance:** a checkpoint whose first probe is indecisive can follow up (e.g.
`dumpsys audio` truncated → `settings get global zen_mode`) and converge, instead
of guessing. The 0-tool-call fast path remains for simple items.

---

### Gap 4 — Node-side verdict hygiene + fail-open release separation

**Upstream mechanism:**
- `verdicts_allow_release(report)`: `all(v.status ∈ {passed,inconclusive} for
  verify items)` — computed **outside** the model; releasing never rewrites a
  verdict value. Assert failures never block release.
- `_normalize_report`: a `failed` verdict with blank evidence → downgraded to
  `inconclusive` ("failed verdict lacked concrete evidence; downgraded"); every
  declared item is guaranteed a verdict (missing → `inconclusive`).
- `CheckReport.unmet_subgoals` (final only): plan subgoals marked `[x]` whose
  verify criteria are demonstrably unmet.
- `resolve_item_status` + `TestSummary`/`RunOutcome` (machine-readable outcome).

**v2 adaptation (cheap, pure functions in `runner/checker.py`):**

1. `normalize_checker_result(parsed, check_items)`: downgrade evidence-less
   `failed` to `inconclusive`; backfill missing per-item verdicts; drop duplicate
   verdicts.
2. `verdicts_allow_release(check_items, verdicts)`: returns
   `all(v.status in {"passed","inconclusive"} for verify items)`.
3. Add `unmet_subgoals: []` to the Checker JSON schema and thread it through
   `call_checker`'s parsed dict.
4. Change every release decision (`_call_checker_with_probes` arbitration + pro
   final Checker) from `parsed["goal_achieved"]` to
   `verdicts_allow_release(parsed["check_items"])`; keep `goal_achieved` purely
   as a human-facing summary. This is what stops a single hallucinated failure
   from flipping `status=checker_failed`.
5. Add `resolve_item_status(kind, records)` + `TestSummary` to
   `runner/post_run_report.py` fed from `memory/check_ledger.jsonl`.

**Acceptance:** unit tests (mirror upstream `test_checker.py`
`test_verdicts_allow_release_semantics`): a report with a blank-evidence
`failed` verify is released (fail-open) but recorded `inconclusive`; a failed
assert never blocks release; every declared item resolves to a status.

---

### Suggested build order & effort

| Step | Gaps | Effort | Depends on |
|---|---|---|---|
| A. `runner/plan_grammar` helpers: `revert_subgoal_status`, finding lines, mutation guards | 2,§9.2(5,6) | small | — |
| B. `normalize_checker_result` + `verdicts_allow_release` + `unmet_subgoals` | **4** | small | — |
| C. `runner/checkpoints.py` (ledger, anchor) + `call_checkpoint_check` | **1** | medium | B |
| D. Checkpoint-hook repair loop in the pro loop | **2** | medium | A,C |
| E. Stage-3a bounded re-probe | **3** | medium | C |
| F. Final-check re-entry loop (`route=continue`) | 2 | medium | A,B |

A+B ship independently and immediately remove the false-negative class; C+D
deliver the actual "state-at-completion" fix; E is a follow-on only if pre-run
probes prove insufficient.