"""Verify bounded reuse of local snapshot round profiles."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
import sys
from threading import Barrier
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from austins_algorithm.ibm_models import _prepare_density_snapshot, _snapshot_identity
from austins_algorithm.unknown_count_snapshot import compare_unknown_count_snapshot
from austins_algorithm.unknown_count_snapshot_cache import SnapshotRoundProfileCache


_SDK_MODULES = ("qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import")
_PLAN = {"policy": "affordable", "max_shots": 3, "max_oracle_queries": 1, "max_rounds_per_shot": 1}


def _run(cache, *, mark="11", seeds=(7, 29), layout=(0, 1), seed_transpiler=11, budget=None):
    from qiskit import QuantumCircuit

    oracle = QuantumCircuit(2)
    if mark == "00":
        oracle.x([0, 1])
    oracle.cz(0, 1)
    if mark == "00":
        oracle.x([0, 1])
    return compare_unknown_count_snapshot(
        oracle, 2, lambda bits: bits == mark,
        plans={"test": _PLAN if budget is None else budget}, seeds=seeds, model="jakarta",
        seed_transpiler=seed_transpiler, cache=cache, initial_layout=layout,
    )


def _semantic(result):
    return {key: value for key, value in result.items() if key != "timing_seconds"}


def _cache_work(result):
    return result["timing_seconds"]["round_profile_cache"]


class SnapshotRoundProfileCacheInputTests(unittest.TestCase):
    def test_new_cache_has_empty_bounded_stats(self):
        cache = SnapshotRoundProfileCache()
        self.assertEqual(cache.stats(), {
            "entries": 0, "bytes": 0, "max_entries": 64, "max_bytes": 8 * 1024 * 1024,
            "evictions": 0, "unconfirmed": False,
        })


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotRoundProfileCacheTests(unittest.TestCase):
    def test_cold_and_warm_match_and_fresh_seeds_reuse_only_the_profiles(self):
        cache = SnapshotRoundProfileCache()
        with patch(
            "austins_algorithm.unknown_count_snapshot._prepare_density_snapshot", wraps=_prepare_density_snapshot,
        ) as prepare:
            cold = _run(cache)
            warm = _run(cache)
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(_semantic(cold), _semantic(warm))
        self.assertEqual((_cache_work(cold)["hits"], _cache_work(cold)["misses"]), (0, 2))
        self.assertEqual((_cache_work(warm)["hits"], _cache_work(warm)["misses"]), (2, 0))
        self.assertEqual((_cache_work(cold)["published"], _cache_work(warm)["published"]), (2, 0))
        self.assertEqual(_cache_work(cold)["keys"], _cache_work(warm)["keys"])
        self.assertEqual(cache.stats()["entries"], 2)

        expected = deepcopy(warm["compilations"])
        cold["compilations"]["0"]["snapshot_probabilities"]["outcome_probabilities"]["00"] = -1
        fresh = _run(cache, seeds=(43, 47))
        self.assertEqual(_cache_work(fresh)["hits"], 2)
        self.assertEqual(fresh["compilations"], expected)
        self.assertEqual(warm["compilations"], expected)
        for lane in fresh["plans"]["test"]["lanes"].values():
            self.assertEqual([trial["seed"] for trial in lane["trials"]], [43, 47])

    def test_cache_keys_include_compiled_circuit_layout_seed_and_snapshot(self):
        cache = SnapshotRoundProfileCache()
        original = _run(cache)
        opposite = _run(cache, mark="00")
        self.assertEqual((_cache_work(opposite)["hits"], _cache_work(opposite)["misses"]), (1, 1))
        self.assertEqual(opposite["audit"]["marked_states"], ["00"])
        before = original["compilations"]["0"]["snapshot_probabilities"]
        after = opposite["compilations"]["0"]["snapshot_probabilities"]
        self.assertEqual(after["outcome_probabilities"], before["outcome_probabilities"])
        self.assertAlmostEqual(after["verified_hit_probability"], after["outcome_probabilities"]["00"], places=12)

        relocated = _run(cache, layout=(1, 0))
        self.assertEqual(relocated["anchor_layout"], [1, 0])
        self.assertEqual((_cache_work(relocated)["hits"], _cache_work(relocated)["misses"]), (0, 2))
        changed_seed = _run(cache, seed_transpiler=23)
        self.assertEqual((_cache_work(changed_seed)["hits"], _cache_work(changed_seed)["misses"]), (0, 2))

        def changed_calibration(backend):
            identity = _snapshot_identity(backend)
            digest = identity["configuration_properties_sha256"]
            return {**identity, "configuration_properties_sha256": ("1" if digest[0] == "0" else "0") + digest[1:]}

        with patch("austins_algorithm.unknown_count_snapshot._snapshot_identity", side_effect=changed_calibration):
            changed_snapshot = _run(cache)
        self.assertEqual((_cache_work(changed_snapshot)["hits"], _cache_work(changed_snapshot)["misses"]), (0, 2))

    def test_concurrent_requests_retain_unique_profiles_and_respect_capacity(self):
        from austins_algorithm.compilation import compile_and_verify

        cache = SnapshotRoundProfileCache(max_entries=2)
        rendezvous = Barrier(2)

        def compare():
            rendezvous.wait(timeout=10)
            return _run(cache)

        with (
            patch("austins_algorithm.unknown_count_snapshot.compile_and_verify", wraps=compile_and_verify) as compile_round,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [executor.submit(compare) for _ in range(2)]
            left, right = [future.result(timeout=30) for future in futures]
        self.assertEqual(compile_round.call_count, 2)
        self.assertEqual(_semantic(left), _semantic(right))
        self.assertEqual(_cache_work(left)["keys"], _cache_work(right)["keys"])
        self.assertEqual(cache.stats()["entries"], 2)
        self.assertEqual(cache.stats()["evictions"], 0)
        self.assertGreater(cache.stats()["bytes"], 0)
        self.assertFalse(cache.stats()["unconfirmed"])

        limited = SnapshotRoundProfileCache(max_entries=1)
        _run(limited)
        self.assertEqual(limited.stats()["entries"], 1)
        self.assertEqual(limited.stats()["evictions"], 1)
        self.assertLessEqual(limited.stats()["bytes"], limited.stats()["max_bytes"])

    def test_unconfirmed_timeout_marks_cache_and_stops_reuse(self):
        cache = SnapshotRoundProfileCache()
        warm_budget = {**_PLAN, "max_oracle_queries": 0}
        _run(cache, budget=warm_budget)
        timeout = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": False, "wall_seconds": 0.0}
        attempts = 0

        def prepare_then_timeout(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            return _prepare_density_snapshot(*args, **kwargs) if attempts == 1 else (None, timeout)

        with patch(
            "austins_algorithm.unknown_count_snapshot._prepare_density_snapshot", side_effect=prepare_then_timeout,
        ):
            failed = _run(cache, seed_transpiler=23)
        self.assertEqual(attempts, 2)
        self.assertEqual(failed["status"], "LOCAL_RESULT_WAIT_TIMED_OUT")
        self.assertFalse(failed["local_job_cancelled"])
        self.assertEqual(_cache_work(failed)["published"], 0)
        self.assertTrue(cache.stats()["unconfirmed"])
        with (
            patch("austins_algorithm.unknown_count_snapshot.compile_and_verify") as compile_round,
            self.assertRaisesRegex(RuntimeError, r"(?i)unconfirmed"),
        ):
            _run(cache, budget=warm_budget)
        compile_round.assert_not_called()


if __name__ == "__main__":
    unittest.main()
