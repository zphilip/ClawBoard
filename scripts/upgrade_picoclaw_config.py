#!/usr/bin/env python3
"""
upgrade_picoclaw_config.py — Upgrade PicoClaw config.json from version 0/1/2 to version 3.

Mirrors the migration logic in picoclaw/pkg/config/migration.go exactly.

V0 → V1:
  - agents.defaults.model  →  agents.defaults.model_name
  - providers map          →  model_list array
  - model_list api_key     →  api_keys (deduplicated)
  - remove providers key   (not in V3 Config struct)
  - version = 1

V1 → V2:
  - channel mention_only   →  group_trigger.mention_only
  - model_list api_key     →  api_keys (deduplicated)
  - model_list: infer enabled=true when api_keys present or model_name=="local-model"
  - version = 2

V2 → V3:
  - remove bindings key
  - remove session.dm_scope (replaced by session.dimensions; not in V3 struct)
  - agents.defaults.model  →  agents.defaults.model_name
  - channels               →  channel_list
  - each channel: group_trigger_prefix  →  group_trigger.prefixes
  - each channel: non-base fields       →  settings sub-object
  - each channel: set type = channel key
  - version = 3

Security YAML (.security.yml, same directory as config.json):
  - channels key  →  channel_list

Usage:
    python3 upgrade_picoclaw_config.py [config_path] [--dry-run] [--verbose]
"""

import argparse
import copy
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# ─── Constants ────────────────────────────────────────────────────────────────

CURRENT_VERSION = 3

# Fields that belong to the Channel base struct — NOT moved into "settings"
BASE_FIELD_NAMES = {
    "enabled",
    "type",
    "allow_from",
    "reasoning_channel_id",
    "group_trigger",
    "typing",
    "placeholder",
}

# V0 provider migration table: mirrors v0ProvidersMapToModelList in config_old.go
# Each entry: (json_keys, protocol, default_model, extra_fields)
#   extra_fields: set of field names to copy beyond api_key/api_base/proxy/request_timeout
_V0_PROVIDER_MIGRATIONS = [
    (["openai", "gpt"],              "openai",        "openai/gpt-5.4",
     {"web_search", "auth_method"}),
    (["anthropic", "claude"],        "anthropic",     "anthropic/claude-sonnet-4.6",
     {"auth_method"}),
    (["litellm"],                    "litellm",       "litellm/auto",              set()),
    (["openrouter"],                 "openrouter",    "openrouter/auto",           set()),
    (["groq"],                       "groq",          "groq/llama-3.1-70b-versatile", set()),
    (["zhipu", "glm"],               "zhipu",         "zhipu/glm-4",               set()),
    (["vllm"],                       "vllm",          "vllm/auto",                 set()),
    (["gemini", "google"],           "gemini",        "gemini/gemini-pro",         set()),
    (["nvidia"],                     "nvidia",        "nvidia/meta/llama-3.1-8b-instruct", set()),
    (["ollama"],                     "ollama",        "ollama/llama3",             set()),
    (["moonshot", "kimi"],           "moonshot",      "moonshot/kimi",             set()),
    (["shengsuanyun"],               "shengsuanyun",  "shengsuanyun/auto",         set()),
    (["deepseek"],                   "deepseek",      "deepseek/deepseek-chat",    set()),
    (["cerebras"],                   "cerebras",      "cerebras/llama-3.3-70b",    set()),
    (["vivgrid"],                    "vivgrid",       "vivgrid/auto",              set()),
    (["volcengine", "doubao"],       "volcengine",    "volcengine/doubao-pro",     set()),
    (["github_copilot", "copilot"],  "github-copilot","github-copilot/gpt-5.4",
     {"connect_mode"}),
    (["antigravity"],                "antigravity",   "antigravity/gemini-2.0-flash",
     {"auth_method"}),
    (["qwen", "tongyi"],             "qwen",          "qwen/qwen-max",             set()),
    (["mistral"],                    "mistral",       "mistral/mistral-small-latest", set()),
    (["avian"],                      "avian",         "avian/deepseek/deepseek-v3.2", set()),
    (["minimax"],                    "minimax",       "minimax/minimax",           set()),
    (["longcat"],                    "longcat",       "longcat/LongCat-Flash-Thinking", set()),
    (["modelscope"],                 "modelscope",    "modelscope/Qwen/Qwen3-235B-A22B-Instruct-2507", set()),
    (["novita"],                     "novita",        "novita/auto",               set()),
]

# For github_copilot and antigravity the standard fields (proxy/request_timeout) are NOT copied
_V0_SKIP_STANDARD_FIELDS = {"github_copilot", "copilot", "antigravity"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _merge_api_keys(*sources) -> list:
    """Merge multiple api_key / api_keys values into a deduplicated list."""
    seen = []
    result = []
    for src in sources:
        if isinstance(src, str) and src.strip():
            k = src.strip()
            if k not in seen:
                seen.append(k)
                result.append(k)
        elif isinstance(src, list):
            for item in src:
                if isinstance(item, str) and item.strip():
                    k = item.strip()
                    if k not in seen:
                        seen.append(k)
                        result.append(k)
    return result


def _is_providers_map_empty(providers: dict) -> bool:
    for prov in providers.values():
        if isinstance(prov, dict):
            if prov.get("api_key") or prov.get("api_base") or \
               prov.get("connect_mode") or prov.get("auth_method"):
                return False
    return True


def _v0_providers_to_model_list(providers: dict, user_provider: str, user_model: str) -> list:
    """Convert V0 providers map → model_list array.  Mirrors v0ProvidersMapToModelList."""
    result = []
    for json_keys, protocol, def_model, extra_fields in _V0_PROVIDER_MIGRATIONS:
        # Find the provider data
        prov_data = None
        for key in json_keys:
            if key in providers and isinstance(providers[key], dict):
                prov_data = providers[key]
                break
        if prov_data is None:
            continue

        # Build entry — standard fields
        entry = {}
        skip_standard = json_keys[0] in _V0_SKIP_STANDARD_FIELDS
        for field in ("api_key", "api_base"):
            if prov_data.get(field):
                entry[field] = prov_data[field]
        if not skip_standard:
            for field in ("proxy", "request_timeout"):
                if prov_data.get(field) is not None:
                    entry[field] = prov_data[field]
        for field in extra_fields:
            if prov_data.get(field) not in (None, "", False):
                entry[field] = prov_data[field]

        if not entry:
            continue

        entry["model_name"] = json_keys[0]

        # Resolve model string
        model_to_use = def_model
        if user_provider and user_model:
            for key in json_keys:
                if user_provider == key:
                    if "/" not in user_model:
                        model_to_use = f"{protocol}/{user_model}"
                    else:
                        model_to_use = user_model
                    break
        entry["model"] = model_to_use

        result.append(entry)
    return result


# ─── Per-version migration functions ──────────────────────────────────────────

def _migrate_agent_defaults_model(m: dict, verbose: bool) -> None:
    """Move agents.defaults.model → agents.defaults.model_name if model_name not set."""
    agents = m.get("agents")
    if not isinstance(agents, dict):
        return
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        return
    model = defaults.get("model")
    if model is None:
        return
    if "model_name" not in defaults:
        if verbose:
            print(f"  agents.defaults.model → model_name = {model!r}")
        defaults["model_name"] = model
    del defaults["model"]


def migrate_v0_to_v1(m: dict, verbose: bool = False) -> None:
    """V0 → V1 migration."""
    ver = m.get("version", 0)
    if ver != 0:
        raise ValueError(f"migrate_v0_to_v1: expected version 0, got {ver}")

    if verbose:
        print("  Step: agents.defaults.model → model_name")
    _migrate_agent_defaults_model(m, verbose)

    # providers → model_list
    if "model_list" not in m:
        providers = m.get("providers")
        if isinstance(providers, dict) and not _is_providers_map_empty(providers):
            user_provider = ""
            user_model = ""
            agents = m.get("agents", {})
            if isinstance(agents, dict):
                defaults = agents.get("defaults", {})
                if isinstance(defaults, dict):
                    user_provider = defaults.get("provider", "")
                    user_model = (defaults.get("model_name") or
                                  defaults.get("model") or "")
            model_list = _v0_providers_to_model_list(providers, user_provider, user_model)
            if model_list:
                if verbose:
                    print(f"  Step: providers → model_list ({len(model_list)} entries)")
                m["model_list"] = model_list

    # model_list: api_key → api_keys
    for entry in m.get("model_list", []):
        if isinstance(entry, dict):
            merged = _merge_api_keys(entry.get("api_key"), entry.get("api_keys"))
            if merged:
                entry["api_keys"] = merged
                entry.pop("api_key", None)

    # Remove the legacy providers key — the V3 Config struct has no providers
    # field and picoclaw's JSON decoder rejects unknown keys.
    if "providers" in m:
        if verbose:
            print("  Step: remove providers key (migrated to model_list)")
        del m["providers"]

    m["version"] = 1
    if verbose:
        print("  → version = 1")


def migrate_v1_to_v2(m: dict, verbose: bool = False) -> None:
    """V1 → V2 migration."""
    ver = m.get("version", 0)
    if ver != 1:
        raise ValueError(f"migrate_v1_to_v2: expected version 1, got {ver}")

    # channels: mention_only → group_trigger.mention_only
    channels = m.get("channels")
    if isinstance(channels, dict):
        for ch_name, ch in channels.items():
            if isinstance(ch, dict) and "mention_only" in ch:
                mention_only = ch.pop("mention_only")
                gt = ch.setdefault("group_trigger", {})
                if isinstance(gt, dict) and "mention_only" not in gt:
                    gt["mention_only"] = mention_only
                if verbose:
                    print(f"  channel {ch_name!r}: mention_only → group_trigger.mention_only")

    # model_list: api_key → api_keys; infer enabled
    for entry in m.get("model_list", []):
        if not isinstance(entry, dict):
            continue
        # merge api_key → api_keys
        merged = _merge_api_keys(entry.get("api_key"), entry.get("api_keys"))
        if merged:
            entry["api_keys"] = merged
            entry.pop("api_key", None)
        # infer enabled
        if "enabled" not in entry:
            api_keys = entry.get("api_keys", [])
            has_keys = isinstance(api_keys, list) and len(api_keys) > 0
            if has_keys or entry.get("model_name") == "local-model":
                entry["enabled"] = True
                if verbose:
                    print(f"  model {entry.get('model_name')!r}: inferred enabled=true")

    m["version"] = 2
    if verbose:
        print("  → version = 2")


def migrate_v2_to_v3(m: dict, verbose: bool = False) -> None:
    """V2 → V3 migration."""
    ver = m.get("version", 0)
    if ver != 2:
        raise ValueError(f"migrate_v2_to_v3: expected version 2, got {ver}")

    # Remove bindings
    if "bindings" in m:
        if verbose:
            print("  Step: remove bindings key")
        del m["bindings"]

    # Remove session.dm_scope — replaced by session.dimensions in V3; the V3
    # Config struct has no dm_scope field and picoclaw's JSON decoder is strict.
    session = m.get("session")
    if isinstance(session, dict) and "dm_scope" in session:
        del session["dm_scope"]
        if verbose:
            print("  Step: remove session.dm_scope (deprecated, replaced by dimensions)")
        if not session:  # drop empty session block
            del m["session"]

    _migrate_agent_defaults_model(m, verbose)

    # channels → channel_list
    channels = m.pop("channels", None)
    if channels is not None:
        if verbose:
            print("  Step: rename channels → channel_list")

        if isinstance(channels, dict):
            for ch_key, ch in channels.items():
                if not isinstance(ch, dict):
                    continue

                # Set type = channel key name
                ch["type"] = ch_key

                # group_trigger_prefix → group_trigger.prefixes
                if "group_trigger_prefix" in ch:
                    gtp = ch.pop("group_trigger_prefix")
                    gt = ch.setdefault("group_trigger", {})
                    if isinstance(gt, dict) and "prefixes" not in gt:
                        gt["prefixes"] = gtp
                    if verbose:
                        print(f"  channel {ch_key!r}: group_trigger_prefix → group_trigger.prefixes")

                # Move non-base fields → settings (only if settings not already present)
                if "settings" not in ch:
                    settings = {}
                    for field in list(ch.keys()):
                        if field not in BASE_FIELD_NAMES:
                            settings[field] = ch.pop(field)
                    if settings:
                        ch["settings"] = settings
                        if verbose:
                            print(f"  channel {ch_key!r}: moved {list(settings)} → settings")

        m["channel_list"] = channels

    m["version"] = CURRENT_VERSION
    if verbose:
        print(f"  → version = {CURRENT_VERSION}")


def sanitize_v3(m: dict, verbose: bool = False) -> bool:
    """Remove fields that are NOT present in the V3 Config struct but may linger in
    configs written by old dashboard versions or picoclaw itself during a partial upgrade.
    Returns True if any key was removed.
    Called for ALL configs, including those already at version 3.
    """
    changed = False

    # agents.defaults.model — legacy V0 key renamed to model_name in V0→V1 migration.
    # Not in V3 AgentDefaults struct; picoclaw's strict decoder rejects it.
    defaults = m.get("agents", {}).get("defaults")
    if isinstance(defaults, dict) and "model" in defaults:
        if "model_name" not in defaults:
            # Salvage: promote to model_name before dropping
            defaults["model_name"] = defaults["model"]
            if verbose:
                print(f"  sanitize: agents.defaults.model → model_name = {defaults['model_name']!r}")
        else:
            if verbose:
                print(f"  sanitize: remove agents.defaults.model (model_name already set)")
        del defaults["model"]
        changed = True

    # session.dm_scope — replaced by session.dimensions in a later refactor.
    # Not in V3 SessionConfig struct; picoclaw's strict decoder rejects it.
    session = m.get("session")
    if isinstance(session, dict) and "dm_scope" in session:
        del session["dm_scope"]
        if verbose:
            print("  sanitize: remove session.dm_scope (replaced by session.dimensions)")
        if not session:
            del m["session"]
        changed = True

    return changed


def migrate_security_yml(sec: dict, verbose: bool = False) -> bool:
    """Rename 'channels' → 'channel_list' in security.yml dict.  Returns True if changed."""
    if "channels" in sec and "channel_list" not in sec:
        if verbose:
            print("  security.yml: rename channels → channel_list")
        sec["channel_list"] = sec.pop("channels")
        return True
    return False


# ─── Top-level upgrade logic ──────────────────────────────────────────────────

def upgrade_config(config_path: str, dry_run: bool = False, verbose: bool = False) -> bool:
    """
    Upgrade config.json (and the companion .security.yml) to version 3.
    Returns True if any change was made.
    """
    config_path = os.path.expanduser(config_path)
    if not os.path.exists(config_path):
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        return False

    # ── Load config.json ──
    with open(config_path, "r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error: failed to parse {config_path}: {e}", file=sys.stderr)
            return False

    version = cfg.get("version", 0)
    if not isinstance(version, int):
        print(f"Error: version field is not an integer: {version!r}", file=sys.stderr)
        return False

    if version > CURRENT_VERSION:
        print(f"Warning: config version {version} is newer than this script (supports up to {CURRENT_VERSION}). Skipping.")
        return False

    if version == CURRENT_VERSION:
        print(f"config.json is already version {CURRENT_VERSION}. Checking for stale fields.")
        # Still check security.yml and run field sanitizer
    else:
        print(f"Migrating config.json: version {version} → {CURRENT_VERSION}")

    # Work on a deep copy so we can show a diff
    new_cfg = copy.deepcopy(cfg)

    # Run migration chain
    try:
        if version == 0:
            if verbose:
                print("V0 → V1:")
            migrate_v0_to_v1(new_cfg, verbose)
            if verbose:
                print("V1 → V2:")
            migrate_v1_to_v2(new_cfg, verbose)
            if verbose:
                print("V2 → V3:")
            migrate_v2_to_v3(new_cfg, verbose)
        elif version == 1:
            if verbose:
                print("V1 → V2:")
            migrate_v1_to_v2(new_cfg, verbose)
            if verbose:
                print("V2 → V3:")
            migrate_v2_to_v3(new_cfg, verbose)
        elif version == 2:
            if verbose:
                print("V2 → V3:")
            migrate_v2_to_v3(new_cfg, verbose)
    except ValueError as e:
        print(f"Error during migration: {e}", file=sys.stderr)
        return False

    # Always sanitize stale fields that are not in the V3 struct — catches configs
    # that were already version 3 but written by older dashboard/migration code.
    if verbose:
        print("Sanitize:")
    sanitize_changed = sanitize_v3(new_cfg, verbose)
    if not sanitize_changed and version == CURRENT_VERSION and verbose:
        print("  (no stale fields found)")

    cfg_changed = (version != CURRENT_VERSION) or sanitize_changed

    # ── Security YAML ──
    config_dir = os.path.dirname(config_path)
    sec_path = os.path.join(config_dir, ".security.yml")
    sec_changed = False
    new_sec = None

    if os.path.exists(sec_path):
        if not HAS_YAML:
            print(f"Warning: PyYAML not installed — skipping {sec_path}")
        else:
            with open(sec_path, "r", encoding="utf-8") as f:
                try:
                    new_sec = yaml.safe_load(f) or {}
                except yaml.YAMLError as e:
                    print(f"Warning: failed to parse {sec_path}: {e} — skipping")
                    new_sec = None

            if new_sec is not None:
                sec_changed = migrate_security_yml(new_sec, verbose)
                if not sec_changed:
                    print(f".security.yml already uses channel_list (or no channel data). No change.")

    # ── Write / report ──
    if dry_run:
        if cfg_changed:
            print(f"\n[dry-run] Would write {config_path}")
            if verbose:
                print(json.dumps(new_cfg, indent=2, ensure_ascii=False))
        if sec_changed:
            print(f"[dry-run] Would write {sec_path}")
            if verbose and new_sec is not None:
                print(yaml.dump(new_sec, allow_unicode=True, default_flow_style=False))
        return cfg_changed or sec_changed

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if cfg_changed:
        backup_path = f"{config_path}.bak.{ts}"
        shutil.copy2(config_path, backup_path)
        print(f"  Backup → {backup_path}")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"  Written → {config_path}")

    if sec_changed and new_sec is not None:
        backup_sec = f"{sec_path}.bak.{ts}"
        shutil.copy2(sec_path, backup_sec)
        print(f"  Backup → {backup_sec}")
        with open(sec_path, "w", encoding="utf-8") as f:
            yaml.dump(new_sec, f, allow_unicode=True, default_flow_style=False)
        print(f"  Written → {sec_path}")

    return cfg_changed or sec_changed


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Upgrade PicoClaw config.json from version 0/1/2 to version 3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="~/.picoclaw/config.json",
        help="Path to config.json (default: ~/.picoclaw/config.json)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would change without writing files",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each migration step in detail",
    )
    args = parser.parse_args()

    changed = upgrade_config(args.config, dry_run=args.dry_run, verbose=args.verbose)
    sys.exit(0 if changed or True else 1)


if __name__ == "__main__":
    main()
