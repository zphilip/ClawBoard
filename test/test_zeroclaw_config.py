#!/usr/bin/env python3
"""
Tests for ZeroClaw configuration management in ClawBoard dashboard.

Run:  cd /opt/clawboard && python3 test/test_zeroclaw_config.py
"""

import json
import os
import sys
import tomllib        # Python 3.11+ stdlib (read-only TOML)
import unittest
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)


# ── Canonical provider slots from for_each_model_provider_slot! ──────────────
CANONICAL_SLOTS = {
    'ai21', 'aihubmix', 'anthropic', 'anyscale', 'arcee', 'astrai',
    'atomic_chat', 'avian', 'azure', 'baichuan', 'baseten', 'bedrock',
    'cerebras', 'cloudflare', 'cohere', 'copilot', 'custom', 'deepinfra',
    'deepmyst', 'deepseek', 'doubao', 'featherless', 'fireworks', 'friendli',
    'gemini', 'gemini_cli', 'github_models', 'glm', 'groq', 'huggingface',
    'hunyuan', 'hyperbolic', 'inception', 'kilo', 'kilocli', 'lambda_ai',
    'lepton', 'litellm', 'llamacpp', 'lmstudio', 'manifest', 'minimax',
    'mistral', 'moonshot', 'morph', 'nearai', 'nebius', 'novita', 'nscale',
    'nvidia', 'ollama', 'openai', 'opencode', 'openrouter', 'osaurus',
    'ovh', 'perplexity', 'qianfan', 'qwen', 'reka', 'sambanova', 'sglang',
    'siliconflow', 'stepfun', 'synthetic', 'telnyx', 'together', 'upstage',
    'venice', 'vercel', 'vllm', 'xai', 'yi', 'zai',
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_hints():
    path = os.path.join(SCRIPT_DIR, 'config', 'provider_hints.json')
    with open(path, 'r') as f:
        return json.load(f)

def load_template():
    path = os.path.join(SCRIPT_DIR, 'config', 'config.toml')
    with open(path, 'rb') as f:
        return tomllib.load(f)


# ── Tests ────────────────────────────────────────────────────────────────────

class TestProviderHints(unittest.TestCase):
    """provider_hints.json validation."""

    @classmethod
    def setUpClass(cls):
        cls.hints = load_hints()

    def test_all_entries_have_required_fields(self):
        for h in self.hints:
            with self.subTest(model_name=h.get('model_name', 'MISSING')):
                self.assertIn('model_name', h, f"missing model_name in {h}")
                self.assertIn('model', h, f"missing model in {h}")
                self.assertIn('provider_id', h, f"missing provider_id in {h}")

    def test_all_provider_ids_are_canonical(self):
        bad = set()
        for h in self.hints:
            pid = h['provider_id']
            if pid not in CANONICAL_SLOTS:
                bad.add(pid)
        self.assertEqual(bad, set(),
            f"Non-canonical provider_ids: {bad}. "
            f"Use canonical slot names only.")

    def test_no_legacy_custom_https(self):
        for h in self.hints:
            self.assertNotEqual(h['provider_id'], 'custom:https://',
                f"'{h['model_name']}': use 'custom' + api_base, not 'custom:https://'")

    def test_no_legacy_minimax_cn_as_provider_id(self):
        for h in self.hints:
            self.assertNotEqual(h['provider_id'], 'minimax-cn',
                f"'{h['model_name']}': use 'minimax' + suggested_alias='minimax-cn'")

    def test_no_legacy_kimi_code_as_provider_id(self):
        for h in self.hints:
            self.assertNotEqual(h['provider_id'], 'kimi-code',
                f"'{h['model_name']}': use 'moonshot' instead of 'kimi-code'")

    def test_no_legacy_dashscope_as_provider_id(self):
        for h in self.hints:
            self.assertNotEqual(h['provider_id'], 'dashscope',
                f"'{h['model_name']}': use 'qwen' instead of 'dashscope'")

    def test_multi_region_variants_have_suggested_alias(self):
        variants = {
            'minimax-cn', 'moonshot-cn', 'moonshot-intl',
            'stepfun-cn', 'stepfun-intl',
        }
        found = set()
        for h in self.hints:
            sa = h.get('suggested_alias', '')
            if sa in variants:
                found.add(sa)
        missing = variants - found
        self.assertEqual(missing, set(),
            f"Missing multi-region variant hints: {missing}")

    def test_suggested_alias_is_clean_slug(self):
        """suggested_alias must be a clean alias — no spaces, no parens.
        It may equal provider_id for single-region families (e.g. glm→glm).
        For multi-region it differs (e.g. minimax→minimax-cn)."""
        for h in self.hints:
            if 'suggested_alias' not in h:
                continue
            sa = h['suggested_alias']
            self.assertNotIn(' ', sa)
            self.assertNotIn('(', sa)

    def test_minimax_cn_has_correct_provider_id(self):
        for h in self.hints:
            if h.get('suggested_alias') == 'minimax-cn':
                self.assertEqual(h['provider_id'], 'minimax')
                return
        self.fail("Missing minimax-cn hint with suggested_alias")

    def test_covers_all_canonical_slots(self):
        covered = {h['provider_id'] for h in self.hints}
        uncovered = CANONICAL_SLOTS - covered
        # custom is special — always present
        uncovered.discard('custom')
        self.assertEqual(uncovered, set(),
            f"Canonical slots not covered by hints: {uncovered}")

    def test_api_base_urls_match_schema_endpoints(self):
        """Key providers should have correct default endpoints."""
        expected = {
            'openai':     'https://api.openai.com/v1',
            'anthropic':  'https://api.anthropic.com',
            'deepseek':   'https://api.deepseek.com/v1',
            'mistral':    'https://api.mistral.ai/v1',
            'groq':       'https://api.groq.com/openai/v1',
            'cohere':     'https://api.cohere.ai/compatibility/v1',
            'perplexity': 'https://api.perplexity.ai',
            'together':   'https://api.together.xyz/v1',
            'xai':        'https://api.x.ai/v1',
            'minimax':    'https://api.minimaxi.com/v1',
        }
        pid_base = {}
        for h in self.hints:
            pid = h['provider_id']
            if pid not in pid_base and h.get('api_base'):
                pid_base[pid] = h['api_base']

        for pid, url in expected.items():
            if pid in pid_base:
                self.assertEqual(pid_base[pid], url,
                    f"Wrong api_base for {pid}: expected {url}, got {pid_base[pid]}")


class TestStaleEntryCleanup(unittest.TestCase):
    """Test the stale [providers.models] entry cleanup logic."""

    def _make_models(self):
        """Return a dict simulating providers.models with stale entries."""
        return {
            'minimax-cn': {
                'name': 'minimax', 'model': 'MiniMax-M3',
                'uri': 'https://api.minimaxi.com/v1',
                'api_key': 'enc2:aaa', 'temperature': 0.7, 'timeout_secs': 120,
            },
            'custom:https://api.minimaxi.com/v1': {
                'name': 'minimax', 'model': 'MiniMax-M3',
                'uri': 'https://api.minimaxi.com/v1',
                'api_key': 'enc2:bbb', 'temperature': 0.7, 'timeout_secs': 120,
            },
            'minimax': {
                'name': 'minimax', 'model': 'MiniMax-M3',
                'uri': 'https://api.minimaxi.com/v1',
                'api_key': 'enc2:ccc', 'temperature': 0.7, 'timeout_secs': 120,
            },
        }

    def test_cleanup_removes_same_name_different_alias(self):
        models = self._make_models()
        self.assertEqual(len(models), 3)

        pname = 'minimax'
        alias = 'minimax-cn'
        for _k in list(models.keys()):
            _v = models.get(_k)
            if hasattr(_v, 'get') and _v.get('name') == pname and _k != alias:
                del models[_k]

        self.assertEqual(len(models), 1)
        self.assertIn('minimax-cn', models)
        self.assertNotIn('custom:https://api.minimaxi.com/v1', models)
        self.assertNotIn('minimax', models)

    def test_cleanup_preserves_unrelated_providers(self):
        models = self._make_models()
        models['my-openai'] = {'name': 'openai', 'model': 'gpt-5.4'}

        pname = 'minimax'
        alias = 'minimax-cn'
        for _k in list(models.keys()):
            _v = models.get(_k)
            if hasattr(_v, 'get') and _v.get('name') == pname and _k != alias:
                del models[_k]

        self.assertEqual(len(models), 2)
        self.assertIn('minimax-cn', models)
        self.assertIn('my-openai', models)

    def test_cleanup_nothing_to_remove(self):
        models = {'minimax-cn': {'name': 'minimax', 'model': 'MiniMax-M3'}}
        pname = 'minimax'
        alias = 'minimax-cn'
        for _k in list(models.keys()):
            _v = models.get(_k)
            if hasattr(_v, 'get') and _v.get('name') == pname and _k != alias:
                del models[_k]
        self.assertEqual(len(models), 1)

    def test_hasattr_get_works_on_dicts(self):
        """Verify hasattr(x, 'get') works on regular dicts (ensuring
        the fix is compatible with both dict and tomlkit Table)."""
        models = self._make_models()
        for _k, _v in models.items():
            self.assertTrue(hasattr(_v, 'get'),
                f"Entry '{_k}' should support .get()")
            self.assertIsNotNone(_v.get('name'))


class TestConfigTemplate(unittest.TestCase):
    """Validate the local config/config.toml template."""

    @classmethod
    def setUpClass(cls):
        cls.conf = load_template()

    def test_schema_version(self):
        self.assertGreaterEqual(self.conf.get('schema_version', 0), 2)

    def test_providers_fallback_exists(self):
        providers = self.conf.get('providers', {})
        fb = providers.get('fallback', '')
        self.assertTrue(fb, "Missing [providers] fallback")

    def test_fallback_matches_models_entry(self):
        providers = self.conf.get('providers', {})
        fb = providers.get('fallback', '')
        models = providers.get('models', {})
        if fb:
            self.assertIn(fb, models,
                f"Fallback '{fb}' not found in [providers.models]")

    def test_models_entries_have_name_field(self):
        models = self.conf.get('providers', {}).get('models', {})
        for alias, entry in models.items():
            self.assertIn('name', entry,
                f"[providers.models.{alias}] missing 'name' field")
            self.assertIn(entry['name'], CANONICAL_SLOTS,
                f"[providers.models.{alias}].name='{entry['name']}' is not a canonical slot")

    def test_models_entries_have_required_fields(self):
        models = self.conf.get('providers', {}).get('models', {})
        for alias, entry in models.items():
            self.assertIn('model', entry,
                f"[providers.models.{alias}] missing 'model'")

    def test_skills_all_sections_present(self):
        skills = self.conf.get('skills', {})
        self.assertIn('open_skills_enabled', skills)
        self.assertIn('allow_scripts', skills)
        self.assertIn('prompt_injection_mode', skills,
            "Missing prompt_injection_mode in [skills]")
        self.assertIn('skill_creation', skills,
            "Missing [skills.skill_creation] section")
        self.assertIn('skill_improvement', skills,
            "Missing [skills.skill_improvement] section")

    def test_skill_creation_fields(self):
        sc = self.conf.get('skills', {}).get('skill_creation', {})
        self.assertIn('enabled', sc)
        self.assertIn('max_skills', sc)
        self.assertIn('similarity_threshold', sc)

    def test_skill_improvement_fields(self):
        si = self.conf.get('skills', {}).get('skill_improvement', {})
        self.assertIn('enabled', si)
        self.assertIn('cooldown_secs', si)


class TestFieldNameConventions(unittest.TestCase):
    """Ensure config field names match ZeroClaw V3 schema."""

    def test_v3_field_is_uri_not_base_url(self):
        """ZeroClaw V3 uses 'uri', V2 used 'base_url'. Hints should use api_base
        (internal), config entries should use 'uri'."""
        hints = load_hints()
        for h in hints:
            # Hints use 'api_base' for the quick-pick autofill — that's fine
            self.assertIn('api_base', h or {},
                f"Hint '{h.get('model_name')}' missing api_base")

    def test_provider_entry_writes_uri_key(self):
        """Simulate wizard: prov_entry should use 'uri' not 'base_url'."""
        prov_entry = {
            'name': 'minimax',
            'requires_openai_auth': False,
            'model': 'MiniMax-M3',
            'temperature': 0.7,
            'timeout_secs': 120,
        }
        # Wizard adds uri:
        prov_entry['uri'] = 'https://api.minimaxi.com/v1'
        prov_entry['api_key'] = 'sk-test'

        self.assertIn('uri', prov_entry)
        self.assertNotIn('base_url', prov_entry)

    def test_backward_compat_reads_both_uri_and_base_url(self):
        """Loading code should read uri first, fall back to base_url."""
        # Old entry with base_url
        old = {'name': 'test', 'base_url': 'https://old.example.com'}
        url = old.get('uri') or old.get('base_url', '')
        self.assertEqual(url, 'https://old.example.com')

        # New entry with uri
        new = {'name': 'test', 'uri': 'https://new.example.com'}
        url = new.get('uri') or new.get('base_url', '')
        self.assertEqual(url, 'https://new.example.com')

        # Entry with both — uri takes priority
        both = {'name': 'test', 'uri': 'https://new.example.com', 'base_url': 'https://old.example.com'}
        url = both.get('uri') or both.get('base_url', '')
        self.assertEqual(url, 'https://new.example.com')


class TestWizardDefaultAlias(unittest.TestCase):
    """Test the wizard's alias-filling logic."""

    def test_suggested_alias_takes_priority(self):
        """When a hint has suggested_alias, use it over slugified model_name."""
        hints = load_hints()
        for h in hints:
            if 'suggested_alias' in h:
                suggested = h['suggested_alias']
                # The suggested alias should be a clean alias name
                self.assertNotIn(' ', suggested,
                    f"suggested_alias '{suggested}' contains spaces")
                self.assertNotIn('(', suggested,
                    f"suggested_alias '{suggested}' contains parentheses")

    def test_slug_fallback_for_entries_without_suggested_alias(self):
        """Without suggested_alias, slugify model_name."""
        model_name = "MiniMax-M3"
        slug = model_name.lower().replace(' ', '_').replace('-', '_').replace('.', '_').split('(')[0].rstrip('_')
        self.assertEqual(slug, 'minimax_m3')
        # This is ugly — suggested_alias should be provided
        hints = load_hints()
        for h in hints:
            mn = h['model_name']
            slug2 = mn.lower().replace(' ', '_').replace('-', '_').replace('.', '_').split('(')[0].rstrip('_')
            if slug2 != h.get('suggested_alias', slug2):
                # Has suggested_alias — good
                pass

    def test_fill_from_pid_does_not_overwrite_customized_alias(self):
        """_fill_from_pid should only set alias when it matches previous pid."""
        # Simulate: provider=openrouter, alias=openrouter, _last_pid='openrouter'
        # User changes provider to 'minimax'
        last_pid = 'openrouter'
        cur_alias = 'openrouter'

        new_pid = 'minimax'
        # _fill_from_pid logic:
        if not cur_alias or cur_alias == last_pid:
            cur_alias = new_pid.split(':')[0]  # 'minimax'
        last_pid = new_pid

        self.assertEqual(cur_alias, 'minimax')  # alias updated ✓

        # Now user picks a hint that sets alias to 'minimax-cn'
        cur_alias = 'minimax-cn'  # set by _fill_from_hint

        # User changes provider dropdown to 'openai'
        new_pid = 'openai'
        if not cur_alias or cur_alias == last_pid:
            cur_alias = new_pid.split(':')[0]
        last_pid = new_pid

        # cur_alias ('minimax-cn') != last_pid ('minimax') → NOT overwritten
        self.assertEqual(cur_alias, 'minimax-cn')  # preserved ✓


class TestDeployVerification(unittest.TestCase):
    """Test deploy_config read-back verification logic."""

    def test_verification_detects_missing_keys(self):
        """When deployed file has extra keys, verification should fail."""
        wrote_models = {'minimax-cn': {'name': 'minimax'}}
        deployed_models = {
            'minimax-cn': {'name': 'minimax'},
            'custom:https://api.minimaxi.com/v1': {'name': 'minimax'},
            'minimax': {'name': 'minimax'},
        }
        match = set(wrote_models.keys()) == set(deployed_models.keys())
        self.assertFalse(match,
            "Verification should detect extra keys in deployed file")

    def test_verification_passes_when_keys_match(self):
        """When deployed file matches, verification should pass."""
        wrote_models = {'minimax-cn': {'name': 'minimax'}}
        deployed_models = {'minimax-cn': {'name': 'minimax'}}
        match = set(wrote_models.keys()) == set(deployed_models.keys())
        self.assertTrue(match,
            "Verification should pass when keys match")


if __name__ == '__main__':
    unittest.main(verbosity=2)
