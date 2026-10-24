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

#: Action types safe to replay verbatim (tap re-locates by element; the rest
#: are idempotent / launch / button presses).  ``finish``/``wait``/``long_press``
#: are excluded by NON_CACHEABLE_ACTIONS in policy.py.
_FASTPATH_TYPES = {"tap", "type", "system_button", "open", "swipe"}

#: Steps whose post-action foreground package is not verified on replay
#: (open launches vary by splash/ad; system buttons navigate anywhere).
_VERIFY_SKIP_TYPES = {"open", "system_button", "swipe", "type"}


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
        self.enabled = bool(self.cfg.get("enabled", False)) and self.mode != "off"
        self.enforce = self.mode == "enforce"
        self.min_score = float(self.cfg.get("min_score", 0.7))
        self.plan_min_successes = int(self.cfg.get("plan_min_successes", 1))
        self.max_replays_per_state = int(self.cfg.get("max_replays_per_state", 2))

        self._record_store: JsonlMemoryStore | None = None
        self._policy: MemoryPolicy | None = None
        self._plan_store: ArtemisPlanStore | None = None
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
            self.log("[MEMORY] disabled (mode=off)")
            return
        data_dir = self.skill_dir / "memory" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._record_store = JsonlMemoryStore(data_dir / "memory.jsonl")
        self._policy = MemoryPolicy(self._record_store, min_score=self.min_score)
        self._plan_store = ArtemisPlanStore(data_dir / "plans.jsonl")
        self.log(f"[MEMORY] enabled mode={self.mode} intent_key={self._intent_key}")
        self.log(f"[MEMORY] action_store={data_dir / 'memory.jsonl'} plan_store={data_dir / 'plans.jsonl'}")

    # ------------------------------------------------------------------ #
    # Per-step state
    # ------------------------------------------------------------------ #

    def describe_state(self, elements: list) -> dict[str, Any]:
        fg_pkg = ""
        try:
            fg_pkg = self.indexer.get_foreground_app() or ""
        except Exception:
            fg_pkg = ""
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
        or None to fall through to the VLM."""
        if not (self.enforce and self._policy):
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
        if out.action is None:
            return None

        action_type = out.action.action_type
        args = dict(out.action.arguments or {})

        # Tap drift-safety: re-locate by stored resource-id when present;
        # otherwise skip (a stale pixel coordinate is unsafe to replay).
        if action_type == "tap":
            rid = args.pop("_rid", "") or ""
            if rid:
                el = find_element_by_rid(elements, rid)
                if el is not None:
                    args["coordinate"] = [el.center[0], el.center[1]]
                    args["target"] = getattr(el, "index", None)
                else:
                    self.log(f"[MEMORY] tap fastpath: element '{rid}' not on screen — skip")
                    return None
            elif not args.get("coordinate"):
                self.log("[MEMORY] tap fastpath: no coordinate and no element id — skip")
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
    ) -> None:
        """Record a successful action to the action store + task plan."""
        if not self.enabled:
            return
        if action_type in ("finish", "wait"):
            return
        args = dict(action_args)
        if action_type == "tap" and target_rid and "_rid" not in args:
            args["_rid"] = target_rid

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

        # Collect a clean plan step (without the internal _rid key).
        self._collected.append(ArtemisPlanStep(
            action_type=action_type,
            action_args={k: v for k, v in args.items() if k != "_rid"},
            description=action_desc,
            target_rid=target_rid,
            post_pkg=post_pkg,
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
            if step.action_type == "tap" and step.target_rid:
                el = find_element_by_rid(elements, step.target_rid)
                if el is not None:
                    args["coordinate"] = [el.center[0], el.center[1]]
                    args["target"] = getattr(el, "index", None)
                elif not args.get("coordinate"):
                    self.log(f"[MEMORY] replay step {i}: target '{step.target_rid}' missing — abort")
                    self._plan_store.increment_plan_fail(self._intent_key)
                    return {"status": "failed", "at_step": i}

            self.execute_fn(step.action_type, args, elements)
            time.sleep(1.0)

            post_pkg = ""
            try:
                post_pkg = self.indexer.get_foreground_app() or ""
            except Exception:
                post_pkg = ""

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