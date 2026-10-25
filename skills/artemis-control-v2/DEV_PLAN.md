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