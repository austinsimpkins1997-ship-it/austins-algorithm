from copy import deepcopy
import http.client
import json
import threading
import unittest

from future_reader.adapters import austin, austin_problem
from future_reader.cli import ACTOR, local_server, operate, release_demo
from future_reader.codec import digest
from future_reader.controller import Controller
from future_reader.decisions import break_even, demo, evaluate
from future_reader.releases import ReleaseStore, manifest


class IntegrationTests(unittest.TestCase):
    def test_authorized_loopback_preview_commit_and_replay(self):
        token = "local-test-token-of-sufficient-length"
        with Controller() as controller:
            controller.set_grant(ACTOR, True)
            server, _ = local_server(controller, 0, token=token)
            observed = {}

            def client():
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)

                def call(request, **headers):
                    connection.request("POST", "/v1/operation", json.dumps(request),
                                       {"Content-Type": "application/json", **headers})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())

                try:
                    observed["anonymous"] = call({"operation": "CHECKPOINT"})
                    auth = {"Authorization": f"Bearer {token}"}
                    observed["origin"] = call({"operation": "PINPOINT"}, Origin="null", **auth)
                    observed["impersonation"] = call({"operation": "PINPOINT", "actor": "other"}, **auth)
                    status, preview = call({"operation": "TRAVEL"}, **auth)
                    observed["preview"] = status, preview["status"]
                    request = {"operation": "COMMIT", "preview_id": preview["preview_id"]}
                    observed["commit"] = call(request, **auth)
                    observed["retry"] = call(request, **auth)
                except Exception as error:
                    observed["error"] = error
                finally:
                    connection.close()

            worker = threading.Thread(target=client)
            worker.start()
            try:
                server.timeout = 1
                for _ in range(6):
                    server.handle_request()
                worker.join(6)
            finally:
                server.server_close()
            self.assertNotIn("error", observed)
            self.assertEqual(observed["anonymous"][0], 403)
            self.assertEqual(observed["origin"][0], 403)
            self.assertEqual(observed["impersonation"][0], 400)
            self.assertEqual(observed["preview"], (200, "PREVIEW"))
            self.assertEqual(observed["commit"], observed["retry"])
            self.assertEqual(operate(controller, {"operation": "PINPOINT"})["state"], "G")

    def test_scenario_probabilities_preserve_dependence_and_hard_constraints(self):
        example = demo()
        report = example["result"]
        self.assertEqual(report["expected_value_choice"], ["wait-for-check"])
        self.assertEqual(report["robust_choice"], ["fallback"])
        act = report["actions"][0]
        self.assertEqual(act["p_success_and_on_time"], {"numerator": 7, "denominator": 10})
        self.assertEqual(act["p_success_given_on_time"], {"numerator": 1, "denominator": 1})
        self.assertEqual(break_even(success=8, failure=-12, fallback=3)["probability"],
                         {"numerator": 3, "denominator": 4})
        actions = deepcopy(example["inputs"])
        actions[1]["hard_eligible"] = False
        self.assertNotIn("wait-for-check", evaluate(actions, deadline=14)["expected_value_choice"])
        self.assertIsNone(evaluate(actions, deadline=0, equality=False)["actions"][0]["p_success_given_on_time"])

    def test_local_austin_binding_declares_eligible_scope(self):
        problem, paths = austin_problem()
        self.assertEqual(len(paths), 2)
        self.assertEqual(problem["clauses"], [[1, 2], [-1, -2]])
        result = austin()
        if result["status"] == "UNAVAILABLE":
            self.assertIn("direct", result["fallback"])
        else:
            self.assertEqual(result["paths"], [["S", "B", "G"]])
            self.assertEqual(result["exact"]["cost"], 3)
        self.assertEqual(austin(now=17)["status"], "NO_ELIGIBLE_PATH")

    def test_release_preview_atomic_rollback_and_digest_rejection(self):
        old, new = b"old local fixture", b"new local fixture"
        baseline, update = manifest(old, "0.1.0"), manifest(new, "0.1.1")
        with ReleaseStore(":memory:") as store:
            p = store.preview(baseline, old, trusted_manifest_sha256=digest(baseline))
            with self.assertRaises(ValueError):
                store.apply(baseline, old, p, trusted_manifest_sha256=digest(baseline), approval_digest="bad")
            self.assertIsNone(store.status()["active"])
            store.apply(baseline, old, p, trusted_manifest_sha256=digest(baseline), approval_digest=p["approval_digest"])
            with self.assertRaises(ValueError):
                store.preview(update, b"tampered", trusted_manifest_sha256=digest(update))
            p = store.preview(update, new, trusted_manifest_sha256=digest(update))
            store.apply(update, new, p, trusted_manifest_sha256=digest(update), approval_digest=p["approval_digest"])
            self.assertEqual(store.active_bytes(), new)
            with self.assertRaises(ValueError):
                store.rollback(expected_generation=1, compatible_checkpoint_schema=update["checkpoint_schema"])
            with self.assertRaises(ValueError):
                store.rollback(expected_generation=2, compatible_checkpoint_schema="bad-schema")
            store.rollback(expected_generation=2, compatible_checkpoint_schema=update["checkpoint_schema"])
            self.assertEqual(store.active_bytes(), old)
        self.assertTrue(release_demo()["verified"])
