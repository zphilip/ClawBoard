"""Memory runtime for artemis-control.

Wires the cross-run memory package (``memory/``) into the agent loop in a
safe, incremental way:

  - ``mode=off``     — memory disabled (identical to pre-memory behaviour)
  - ``mode=record``  — observe: record successful actions + task plans, but
                       never replay (no behaviour change on the hot path)
  - ``mode=enforce`` — record + replay: plan replay at run start, and
                       action-level fastpath before every VLM decision

The runtime is intentionally decoupled from the VLM / Planner / Validator —
it only knows ADB execution (``execute_fn``) and the element indexer.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

from memory import (
    ActionCandidate,
    ArtemisPlan,
    ArtemisPlanStep,
    ArtemisPlanStore,
    DecisionInput,
    JsonlMemoryStore,
    MemoryPolicy,
    MemoryRecord,
    StateSignature,
    build_canonical_intent_key,
    build_intent_signature,
    build_state_key,
    build_ui_components,
    build_ui_fingerprint,
    plan_is_healthy,
)
from runner.notes import (
    append_note_content,
    list_notes_info,
    read_note_content,
    save_note_content,
    update_note_content,
)

#: Scratchpad persists as a JSON blob (not a .md note) so ``list_notes_keys``
#: never picks it up as a user-facing note.
_SCRATCHPAD_FILENAME = "_scratchpad.json"

#: Maximum scratchpad keys kept across runs (oldest dropped when exceeded).
_DEFAULT_SCRATCHPAD_MAX_KEYS = 50

#: Action types safe to replay verbatim (tap re-locates by element; the rest
#: are idempotent / launch / button presses).  ``finish``/``wait``/``long_press``
#: are excluded by NON_CACHEABLE_ACTIONS in policy.py.
_FASTPATH_TYPES = {"tap", "type", "system_button", "open", "swipe"}

#: Steps whose post-action foreground package is not verified on replay
#: (open launches vary by splash/ad; system buttons navigate anywhere).
_VERIFY_SKIP_TYPES = {"open", "system_button", "swipe", "type"}

#: Descriptions emitted when the Operator's VLM output was broken and the
#: agent fell back to ``system_button: back``.  These are not real decisions
#: and must not be learned as plan steps (a stray "back" is harmful on replay).
_VLM_FAIL_DESC_PREFIXES = (
    "no JSON in response",
    "VLM parse error",
    "truncated:",
    "VLM failure",
)


def ui_summary_from_elements(elements: list, max_nodes: int = 40) -> str:
    """Render artemis ``ScreenElement`` objects into the exact line format
    ``[Class] "label" id=short_id clickable`` that ``build_ui_components`` /
    ``build_ui_fingerprint`` parse.

    Keeps parity with mobile-control's ``summarise_ui_dump`` so the already
    tested (and Tier-1 ad-robust) fingerprint code works unchanged.
    """
    lines: list[str] = []
    for e in elements:
        cls = getattr(e, "class_short", "") or (getattr(e, "class_name", "") or "").rsplit(".", 1)[-1]
        label = (getattr(e, "text", "") or getattr(e, "content_desc", "") or "").strip()
        rid = getattr(e, "resource_id", "") or ""
        short_id = rid.rsplit("/", 1)[-1] if "/" in rid else rid
        parts: list[str] = []
        if cls:
            parts.append(f"[{cls}]")
        if label:
            parts.append(f'"{label[:40]}"')
        if short_id:
            parts.append(f"id={short_id}")
        flags: list[str] = []
        if getattr(e, "clickable", False):
            flags.append("clickable")
        if getattr(e, "long_clickable", False):
            flags.append("long-clickable")
        if flags:
            parts.append(" ".join(flags))
        if len(parts) > 1:
            lines.append("  " + "  ".join(parts))
        if len(lines) >= max_nodes:
            break
    if not lines:
        return ""
    return "UI elements on screen:\n" + "\n".join(lines)


def find_element_by_rid(elements: list, rid: str):
    """Locate an element by full or short resource-id (drift-robust tap)."""
    rid = rid or ""
    if not rid:
        return None
    for el in elements:
        if el.resource_id == rid:
            return el
    short = rid.rsplit("/", 1)[-1] if "/" in rid else rid
    for el in elements:
        el_short = el.resource_id.rsplit("/", 1)[-1] if "/" in el.resource_id else el.resource_id
        if short and el_short == short:
            return el
    return None


def _center_dist_sq(el, coord) -> float:
    """Squared pixel distance from an element center to a historical coord."""
    if not coord:
        return 0.0
    try:
        cx, cy = el.center[0], el.center[1]
        return (cx - coord[0]) ** 2 + (cy - coord[1]) ** 2
    except (TypeError, IndexError, AttributeError):
        return float("inf")


def find_element_by_compound(
    elements: list,
    *,
    rid: str = "",
    class_name: str = "",
    text: str = "",
    content_desc: str = "",
    coord: list | None = None,
):
    """Relocate a recorded element with a multi-level compound key (Tier 1.5).

    Tries, in stability order:
      1. resource-id        — exact, then short (drop package prefix)
      2. class_name + text  — e.g. android.widget.Button + "登录"; matches
                              ``el.text`` OR ``el.content_desc`` carrying it
      3. content_desc       — accessibility label only
    Ties at levels 2/3 break by nearest center to the historical coordinate,
    so when the screen scrolled the right instance of a repeated label wins.

    Returns the matched element or None.
    """
    if not elements:
        return None

    # Level 1 — resource id (exact, then short), disambiguated by label/coord.
    if rid:
        short = rid.rsplit("/", 1)[-1] if "/" in rid else rid
        rid_cands: list[tuple[int, object]] = []
        for el in elements:
            el_rid = el.resource_id or ""
            if el_rid == rid:
                rid_cands.append((0, el))
            elif short and el_rid.rsplit("/", 1)[-1] == short:
                rid_cands.append((1, el))
        if rid_cands:
            # Generic ids like ``android:id/title`` appear on EVERY list row, so
            # a bare rid match would grab the wrong row. Prefer the rid match
            # whose label carries the recorded text/content_desc.
            want = (text or content_desc).strip()
            if want:
                for _, el in rid_cands:
                    el_label = (
                        (el.text or "").strip()
                        or (el.content_desc or "").strip()
                    )
                    if el_label and el_label == want:
                        return el
            # No label match — closest to the historical coordinate wins.
            return min(
                (el for _, el in rid_cands),
                key=lambda e: _center_dist_sq(e, coord),
            )

    # Level 2 — class + text
    if class_name and text:
        cls_short = class_name.rsplit(".", 1)[-1] if class_name else ""
        cands = []
        for el in elements:
            el_cls = (getattr(el, "class_name", "") or "").rsplit(".", 1)[-1]
            el_label = (
                (getattr(el, "text", "") or "").strip()
                or (getattr(el, "content_desc", "") or "").strip()
            )
            if cls_short and el_cls != cls_short:
                continue
            if el_label and el_label == text:
                cands.append(el)
        if cands:
            return min(cands, key=lambda e: _center_dist_sq(e, coord))

    # Level 3 — content-desc only
    if content_desc:
        cands = [
            el for el in elements
            if (getattr(el, "content_desc", "") or "") == content_desc
        ]
        if cands:
            return min(cands, key=lambda e: _center_dist_sq(e, coord))

    return None


def derive_foreground_pkg(elements: list) -> str:
    """Infer the foreground package from the element tree when dumpsys returns
    nothing (artemis ScreenElements carry their own ``package`` field)."""
    counts = Counter(
        (getattr(e, "package", "") or "")
        for e in elements
        if (getattr(e, "package", "") or "")
    )
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


class MemoryRuntime:
    def __init__(
        self,
        *,
        instruction: str,
        memory_cfg: dict[str, Any],
        skill_dir: str | Path,
        execute_fn: Callable[[str, dict, list], None],
        indexer,
        log_fn: Callable[[str], None] = print,
    ):
        self.instruction = instruction
        self.cfg = memory_cfg or {}
        self.skill_dir = Path(skill_dir)
        self.execute_fn = execute_fn
        self.indexer = indexer
        self.log = log_fn

        self.mode = str(self.cfg.get("mode", "record")).lower()
        # ``enabled`` is optional: when omitted, infer it from ``mode`` so a
        # config that only sets mode=record/enforce still activates memory.
        explicit_enabled = self.cfg.get("enabled", None)
        if explicit_enabled is None:
            self.enabled = self.mode != "off"
        else:
            self.enabled = bool(explicit_enabled) and self.mode != "off"
        self.enforce = self.mode == "enforce"
        self.min_score = float(self.cfg.get("min_score", 0.7))
        self.plan_min_successes = int(self.cfg.get("plan_min_successes", 1))
        self.max_replays_per_state = int(self.cfg.get("max_replays_per_state", 2))

        self._record_store: JsonlMemoryStore | None = None
        self._policy: MemoryPolicy | None = None
        self._plan_store: ArtemisPlanStore | None = None
        self._notes_dir: Path | None = None
        self._intent_sig = ""
        self._intent_key = ""
        self._collected: list[ArtemisPlanStep] = []
        self._replayed_sigs: set[str] = set()
        self._replay_counts: dict[str, int] = {}
        self._replay_in_progress = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def setup(self) -> None:
        self._intent_sig = build_intent_signature(self.instruction)
        self._intent_key = build_canonical_intent_key(self.instruction)
        if not self.enabled:
            self.log(
                f"[MEMORY] disabled — enabled={self.enabled} "
                f"cfg.enabled={self.cfg.get('enabled')!r} "
                f"mode={self.mode!r} (no recording, no replay)"
            )
            return
        data_dir = self.skill_dir / "memory" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._record_store = JsonlMemoryStore(data_dir / "memory.jsonl")
        self._policy = MemoryPolicy(self._record_store, min_score=self.min_score)
        self._plan_store = ArtemisPlanStore(data_dir / "plans.jsonl")

        # Persistent notes (Phase 1) — default <skill>/memory/notes.
        notes_override = str(self.cfg.get("notes_dir", "") or "").strip()
        self._notes_dir = (
            Path(notes_override).expanduser()
            if notes_override
            else self.skill_dir / "memory" / "notes"
        )
        self._notes_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"[MEMORY] notes → {self._notes_dir}")

        self.log(
            f"[MEMORY] enabled mode={self.mode} min_score={self.min_score} "
            f"plan_min_successes={self.plan_min_successes} intent_key={self._intent_key}"
        )
        self.log(
            "[MEMORY] ENFORCE: plan-replay + action-fastpath active"
            if self.enforce
            else "[MEMORY] RECORD-ONLY: observing (no replay) — set mode=enforce to reuse"
        )
        self.log(f"[MEMORY] stores → {data_dir / 'memory.jsonl'} | {data_dir / 'plans.jsonl'}")

    # ------------------------------------------------------------------ #
    # Per-step state
    # ------------------------------------------------------------------ #

    def fg_pkg(self, elements: list) -> str:
        """Foreground package: dumpsys first, element-majority fallback."""
        try:
            pkg = self.indexer.get_foreground_app() or ""
        except Exception:
            pkg = ""
        return pkg or derive_foreground_pkg(elements)

    def describe_state(self, elements: list) -> dict[str, Any]:
        fg_pkg = self.fg_pkg(elements)
        ui_summary = ui_summary_from_elements(elements)
        ui_fp = build_ui_fingerprint(fg_pkg, ui_summary)
        comp = build_ui_components(fg_pkg, ui_summary)
        state_key = build_state_key(self._intent_sig, ui_fp, "default")
        return {
            "state_key": state_key,
            "ui_fp": ui_fp,
            "ui_summary": ui_summary,
            "fg_pkg": fg_pkg,
            "rids": sorted(comp.resource_ids),
            "texts": sorted(comp.labels),
        }

    # ------------------------------------------------------------------ #
    # Action fastpath (before VLM)
    # ------------------------------------------------------------------ #

    def fastpath(self, state: dict[str, Any], elements: list) -> Optional[tuple[str, dict, str]]:
        """Return (action_type, action_args, desc) on a confident cache hit,
        or None to fall through to the VLM.

        In ``record`` mode this performs a read-only assessment and logs the
        would-be hit/miss (handy for tuning ``min_score`` before flipping to
        ``enforce``), but never returns an action.
        """
        if not self.enabled or not self._policy:
            return None
        dinput = DecisionInput(
            state=StateSignature(
                foreground_pkg=state["fg_pkg"],
                ui_fingerprint=state["ui_fp"],
                intent_signature=self._intent_sig,
                device_bucket="default",
                resource_ids=state["rids"],
                texts=state["texts"],
            ),
            proposed_action=None,
        )
        try:
            out = self._policy.decide(
                state["state_key"], self._intent_sig, dinput,
                exclude_sigs=self._replayed_sigs,
            )
        except Exception as e:
            self.log(f"[MEMORY] fastpath decision error ({e!r}) — fallback to VLM")
            return None

        score = (out.diagnostics or {}).get("score", None)
        score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "-"

        if out.action is None:
            if self.enforce:
                self.log(f"[MEMORY] fastpath miss ({out.reason}) — VLM will decide")
            else:
                self.log(f"[MEMORY] (record) assess miss ({out.reason})")
            return None

        action_type = out.action.action_type
        if not self.enforce:
            self.log(
                f"[MEMORY] (record) assess WOULD-HIT score={score_s} "
                f"action={action_type} (record mode never replays)"
            )
            return None

        args = dict(out.action.arguments or {})

        # Tap drift-safety: re-locate by compound key (Tier 1.5) — resource-id
        # first, then class+text, then content-desc — instead of replaying a
        # stale pixel coordinate.
        if action_type == "tap":
            rid = args.pop("_rid", "") or ""
            cls_name = args.pop("_cls", "") or ""
            txt = args.pop("_text", "") or ""
            cd = args.pop("_cd", "") or ""
            # A tap is only safe to replay when it can be re-located from the
            # CURRENT element tree.  A bare (coordinate-only) record predates
            # Tier 1.5 and its pixel is post-keyboard / post-ad stale — replaying
            # it lands taps on the wrong element (e.g. the on-screen keyboard).
            relocatable = rid or (cls_name and txt) or cd
            if relocatable:
                el = find_element_by_compound(
                    elements, rid=rid, class_name=cls_name,
                    text=txt, content_desc=cd, coord=args.get("coordinate"),
                )
                if el is not None:
                    args["coordinate"] = [el.center[0], el.center[1]]
                    args["target"] = getattr(el, "index", None)
                else:
                    key = rid or f"{cls_name}+{txt}" or cd
                    self.log(f"[MEMORY] tap fastpath: element '{key}' not on screen — skip")
                    return None
            else:
                self.log(
                    "[MEMORY] tap fastpath: no relocatable key (bare coordinate) "
                    "— fallback to VLM"
                )
                return None

        # Replay cap: if the exact same cached action has already been
        # replayed this run (screen likely stuck), fall back to the VLM so it
        # can see the loop-detection notes and pick a different approach.
        sig = (
            f"{state['state_key']}|{action_type}|"
            f"{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
        )
        if self._replay_counts.get(sig, 0) >= self.max_replays_per_state:
            self.log("[MEMORY] fastpath: replay cap reached — fallback to VLM")
            return None
        self._replay_counts[sig] = self._replay_counts.get(sig, 0) + 1
        self._replayed_sigs.add(sig)

        desc = out.action.action_description or (
            f"{action_type}({args})"[:120]
        )
        desc = f"{desc} [memory]"
        self.log(f"[MEMORY] fastpath HIT score={score_s} action={action_type}")
        return action_type, args, desc

    # ------------------------------------------------------------------ #
    # Recording (after a successfully executed step)
    # ------------------------------------------------------------------ #

    def record_action(
        self,
        state: dict[str, Any],
        action_type: str,
        action_args: dict,
        action_desc: str,
        target_rid: str = "",
        post_pkg: str = "",
        target_el=None,
    ) -> None:
        """Record a successful action to the action store + task plan.

        ``target_el`` (optional ScreenElement) supplies the Tier 1.5 compound
        key (class_name / text / content_desc) for tap steps, so replay can
        relocate an element that had no resource-id.
        """
        if not self.enabled:
            return
        if action_type in ("finish", "wait"):
            return
        if action_desc and action_desc.startswith(_VLM_FAIL_DESC_PREFIXES):
            self.log(
                f"[MEMORY] skip recording fallback step ({action_type}) — "
                f"desc={str(action_desc)[:48]!r} (VLM failure, not a real decision)"
            )
            return
        cls_name = getattr(target_el, "class_name", "") or ""
        txt = getattr(target_el, "text", "") or ""
        cd = getattr(target_el, "content_desc", "") or ""

        args = dict(action_args)
        if action_type == "tap":
            if target_rid and "_rid" not in args:
                args["_rid"] = target_rid
            if cls_name and "_cls" not in args:
                args["_cls"] = cls_name
            if txt and "_text" not in args:
                args["_text"] = txt
            if cd and "_cd" not in args:
                args["_cd"] = cd

        if self._record_store and action_type in _FASTPATH_TYPES:
            try:
                self._record_store.append(MemoryRecord(
                    state_key=state["state_key"],
                    intent_key=self._intent_sig,
                    action_type=action_type,
                    action_args=args,
                    success_count=1,
                    fail_count=0,
                    forbidden=False,
                    reason="step_ok",
                    foreground_pkg=state["fg_pkg"],
                    resource_ids=state["rids"],
                    texts=state["texts"],
                ))
            except Exception as e:
                self.log(f"[MEMORY] record action error ({e!r})")

        self.log(
            f"[MEMORY] recorded step: {action_type} "
            f"rid={target_rid or '-'} cls={cls_name or '-'} "
            f"text={txt[:24] or '-'} post={post_pkg or '-'}"
        )

        # Collect a clean plan step (internal _rid/_cls/_text/_cd keys stripped;
        # compound key lives in the dedicated ArtemisPlanStep fields instead).
        self._collected.append(ArtemisPlanStep(
            action_type=action_type,
            action_args={k: v for k, v in args.items() if not k.startswith("_")},
            description=action_desc,
            target_rid=target_rid,
            post_pkg=post_pkg,
            class_name=cls_name,
            text=txt,
            content_desc=cd,
        ))

    # ------------------------------------------------------------------ #
    # Task-plan replay (run start)
    # ------------------------------------------------------------------ #

    def try_plan_replay(self, emit_fn: Callable[[int, str, str], None] | None = None) -> dict[str, Any]:
        """Attempt to replay a stored plan for this intent.

        Returns ``{"status": "completed"|"no_plan"|"failed"|"disabled", ...}``.
        """
        if not self.enabled or not self._plan_store:
            return {"status": "disabled"}
        if not self.enforce:
            return {"status": "no_plan"}
        plan = self._plan_store.find_best(self._intent_key)
        if plan is None or plan.success_count < self.plan_min_successes:
            return {"status": "no_plan"}

        self.log(f"[MEMORY] plan replay start: intent={self._intent_key} steps={len(plan.steps)}")
        actions: list[str] = []
        for i, step in enumerate(plan.steps):
            elements = self.indexer.build_index()
            args = dict(step.action_args)
            if step.action_type == "tap":
                relocatable = (
                    step.target_rid
                    or (step.class_name and step.text)
                    or step.content_desc
                )
                if not relocatable:
                    # A tap step without any re-locatable key (bare pixel
                    # coordinate) predates Tier 1.5 and is unsafe to replay —
                    # its pixel is stale (keyboard up, ad loaded, scrolled).
                    self.log(
                        f"[MEMORY] replay step {i}: tap has no relocatable key "
                        f"(bare coordinate) — aborting replay"
                    )
                    self._plan_store.increment_plan_fail(self._intent_key)
                    return {"status": "failed", "at_step": i}
                el = find_element_by_compound(
                    elements,
                    rid=step.target_rid,
                    class_name=step.class_name,
                    text=step.text,
                    content_desc=step.content_desc,
                    coord=args.get("coordinate"),
                )
                if el is not None:
                    args["coordinate"] = [el.center[0], el.center[1]]
                    args["target"] = getattr(el, "index", None)
                elif not args.get("coordinate"):
                    key = (
                        step.target_rid
                        or f"{step.class_name}+{step.text}"
                        or step.content_desc
                        or "-"
                    )
                    self.log(f"[MEMORY] replay step {i}: target '{key}' missing — abort")
                    self._plan_store.increment_plan_fail(self._intent_key)
                    return {"status": "failed", "at_step": i}

            self.execute_fn(step.action_type, args, elements)
            time.sleep(1.0)

            # Post-step verification uses foreground package as the signal.
            # dumpsys first, element-majority fallback when it returns empty.
            post_pkg = self.fg_pkg(elements)

            if step.action_type not in _VERIFY_SKIP_TYPES and step.post_pkg and post_pkg:
                if post_pkg != step.post_pkg:
                    self.log(
                        f"[MEMORY] replay verify failed step {i}: "
                        f"expected {step.post_pkg!r} got {post_pkg!r} — abort"
                    )
                    self._plan_store.increment_plan_fail(self._intent_key)
                    return {"status": "failed", "at_step": i}

            actions.append(step.description or f"{step.action_type}")
            if emit_fn:
                emit_fn(i + 1, step.description or step.action_type, f"replay step {i + 1}/{len(plan.steps)}")

        self._mark_plan_success(plan)
        self.log(f"[MEMORY] plan replay completed ({len(plan.steps)} steps)")
        return {"status": "completed", "steps": len(plan.steps), "actions": actions}

    def _mark_plan_success(self, replayed: ArtemisPlan) -> None:
        """Bump the plan's success_count and last_verified timestamp in-place."""
        if not self._plan_store:
            return
        plans = self._plan_store.load()
        hit = False
        for p in plans:
            if p.intent_key == replayed.intent_key and p.instruction_sample == replayed.instruction_sample:
                p.success_count += 1
                p.last_verified = time.time()
                hit = True
        if not hit:
            replayed.success_count += 1
            replayed.last_verified = time.time()
            plans.append(replayed)
        self._plan_store._rewrite(plans)

    # ------------------------------------------------------------------ #
    # Run end
    # ------------------------------------------------------------------ #

    def on_run_end(self, status: str) -> None:
        """Persist a new task plan when the run succeeded."""
        if not self.enabled or not self._plan_store:
            return
        if status != "success" or not self._collected:
            return
        prev_success = 0
        prev_sample = ""
        for p in self._plan_store.load():
            if p.intent_key == self._intent_key:
                prev_success = max(prev_success, p.success_count)
                if p.instruction_sample:
                    prev_sample = p.instruction_sample
        plan = ArtemisPlan(
            intent_key=self._intent_key,
            instruction_sample=prev_sample or self.instruction,
            steps=self._collected,
            success_count=prev_success + 1,
            last_verified=time.time(),
        )
        try:
            self._plan_store.upsert(plan)
            self.log(f"[MEMORY] plan upserted: intent={self._intent_key} steps={len(plan.steps)} successes={plan.success_count}")
        except Exception as e:
            self.log(f"[MEMORY] plan upsert error ({e!r})")

    # ------------------------------------------------------------------ #
    # Persistent notes (Phase 1)
    # ------------------------------------------------------------------ #

    def _notes_base(self) -> Path:
        """Base dir for the notes store.  Raises if memory is disabled."""
        if self._notes_dir is None:
            raise RuntimeError("notes unavailable — memory is disabled")
        return self._notes_dir

    def save_note(self, key: str, content: str) -> None:
        """Create / overwrite a persistent .md note (``task_plan`` → task_plan.md)."""
        save_note_content(self._notes_base(), key, content)

    def read_note(self, key: str, start_line: int | None = None,
                  end_line: int | None = None) -> str:
        """Read a note, optionally a 1-indexed inclusive line range."""
        return read_note_content(self._notes_base(), key, start_line, end_line)

    def append_note(self, key: str, content: str) -> None:
        """Append text to a note (created if missing)."""
        append_note_content(self._notes_base(), key, content)

    def update_note(self, key: str, target: str, replacement: str) -> bool:
        """Replace the first occurrence of *target*; False if not found."""
        return update_note_content(self._notes_base(), key, target, replacement)

    def list_notes(self) -> dict[str, int]:
        """Return {note_key: line_count} for every stored .md note."""
        return list_notes_info(self._notes_base())

    # ── Cross-run scratchpad (JSON blob, separate from .md notes) ───────

    def load_scratchpad(self) -> dict[str, str]:
        """Reload the scratchpad persisted by a previous run."""
        if self._notes_dir is None:
            return {}
        f = self._notes_dir / _SCRATCHPAD_FILENAME
        if not f.exists():
            return {}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {str(k): str(v) for k, v in data.items() if v}
        except Exception as e:
            self.log(f"[MEMORY] scratchpad load error ({e!r})")
            return {}

    def persist_scratchpad(self, scratchpad: dict[str, str]) -> None:
        """Merge + persist the in-run scratchpad for the next run."""
        if self._notes_dir is None:
            return
        if not self.cfg.get("persist_scratchpad", True):
            return
        if not scratchpad:
            return
        try:
            self._notes_dir.mkdir(parents=True, exist_ok=True)
            f = self._notes_dir / _SCRATCHPAD_FILENAME
            merged = self.load_scratchpad()
            merged.update({str(k): str(v) for k, v in scratchpad.items() if v})
            max_keys = int(self.cfg.get("scratchpad_max_keys", _DEFAULT_SCRATCHPAD_MAX_KEYS) or _DEFAULT_SCRATCHPAD_MAX_KEYS)
            if max_keys > 0 and len(merged) > max_keys:
                # Drop oldest keys (dict preserves insertion order).
                merged = dict(list(merged.items())[-max_keys:])
            f.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log(f"[MEMORY] scratchpad persisted ({len(merged)} keys)")
        except Exception as e:
            self.log(f"[MEMORY] scratchpad persist error ({e!r})")