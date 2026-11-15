"""Review bounded plans against named scenarios and caller-recorded evidence."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from itertools import product
import json
from pathlib import Path
import re

from .constraints import MAX_CLAUSES
from .optimization import MAX_VARIABLES, MAX_WEIGHT, exact_minimum
from .workbench_project import _read_regular


DECISION_SCHEMA = "AustinDecisionReview/1"
REPORT_SCHEMA = "AustinDecisionResult/1"
MAX_DECISION_BYTES = 96 * 1024
MAX_SCENARIOS = 8
MAX_CHECKS = 32
UNASSIGNED_COST_UNIT = "Unassigned · all numeric costs are placeholders"
ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
EVIDENCE_STATUSES = ("PASS", "FAIL", "UNKNOWN")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _object(value, fields, label):
    _require(isinstance(value, dict) and value.keys() == set(fields.split()), f"Check fields for {label}.")


def _text(value, label, limit=512):
    _require(isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
             and all(char.isprintable() for char in value), f"Provide printable text for {label}, up to {limit} characters.")


def _sequence(value, label, maximum, minimum=0):
    _require(isinstance(value, list) and minimum <= len(value) <= maximum,
             f"Provide {minimum} to {maximum} entries for {label}.")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _index(records, label, maximum, minimum=0):
    _sequence(records, label, maximum, minimum)
    result = {}
    for record in records:
        key = record.get("id") if isinstance(record, dict) else None
        _require(isinstance(key, str) and ID.fullmatch(key) and key not in result,
                 f"Use unique lowercase identifiers for {label}.")
        result[key] = record
    return result


def _references(value, index, label):
    _sequence(value, label, len(index))
    _require(all(isinstance(key, str) and key in index for key in value) and len(value) == len(set(value)),
             f"Use unique existing IDs for {label}.")


def _dependency_order(index, label):
    visited = set()
    active = set()
    ordered = []

    def visit(key):
        _require(key not in active, f"Cycle in {label} at {key}.")
        if key in visited:
            return
        active.add(key)
        for dependency in index[key]["depends_on"]:
            visit(dependency)
        active.remove(key)
        visited.add(key)
        ordered.append(key)

    for key in index:
        visit(key)
    return ordered


def _validate_check(key, check, checks, revision):
    _object(check, "id label owner status reference revision assessment depends_on basis", key)
    for field in ("label", "owner"):
        _text(check[field], f"{key} {field}")
    _require(check["status"] in EVIDENCE_STATUSES, f"Choose PASS, FAIL or UNKNOWN for {key}.")
    _text(check["reference"], f"{key} evidence reference")
    _require(type(check["revision"]) is int and 1 <= check["revision"] <= revision,
             f"Check {key} observation revision must be at or before the project revision.")
    _require(type(check["assessment"]) is int and 1 <= check["assessment"] <= 2**31 - 1,
             f"Check {key} needs a positive observation sequence.")
    _references(check["depends_on"], checks, f"{key} evidence prerequisites")
    basis = check["basis"]
    _require(isinstance(basis, dict) and basis.keys() <= set(check["depends_on"])
             and all(isinstance(value, str) and SHA256.fullmatch(value) for value in basis.values()),
             f"Check {key} basis must pin named upstream evidence by SHA-256.")


def _validate_stage(key, stage, choices, checks):
    _object(stage, "id name cost depends_on starting_state change expected stop recovery interfaces checks", key)
    _text(stage["name"], f"{key} name", 60)
    _require(type(stage["cost"]) is int and 0 <= stage["cost"] <= MAX_WEIGHT,
             f"Use an integer cost from 0 to {MAX_WEIGHT} for {key}.")
    for field in ("starting_state", "change", "expected", "stop", "recovery"):
        _text(stage[field], f"{key} {field}")
    _references(stage["depends_on"], choices, f"{key} stage dependencies")
    _references(stage["checks"], checks, f"{key} checks")
    _sequence(stage["interfaces"], f"{key} interfaces", 6, 1)
    for interface in stage["interfaces"]:
        _object(interface, "reference tolerance owner", f"{key} interface")
        for field in interface:
            _text(interface[field], f"{key} interface {field}")


def _validate_scenario(key, scenario, choices, checks):
    _object(scenario, "id title rules checks", key)
    _text(scenario["title"], f"{key} title", 120)
    _require(key != "dependencies", "The scenario ID 'dependencies' is reserved for stage rules.")
    _references(scenario["checks"], checks, f"{key} required checks")
    signed = choices.keys() | {f"-{name}" for name in choices}
    for rule_id, rule in _index(scenario["rules"], f"{key} rules", MAX_CLAUSES).items():
        _object(rule, "id literals reason source", f"{key}/{rule_id}")
        _sequence(rule["literals"], f"{key}/{rule_id} choices", 2 * MAX_VARIABLES)
        _require(all(isinstance(literal, str) and literal in signed for literal in rule["literals"]),
                 f"Use signed choice IDs in rule {key}/{rule_id}.")
        _text(rule["reason"], f"{key}/{rule_id} reason")
        _text(rule["source"], f"{key}/{rule_id} source")


def _validate_handoff(handoff, checks):
    owners = "decision_owner delivery_owner verifier resources"
    _object(handoff, f"affected {owners} authority_check consent_check funding_check", "handoff")
    for field in owners.split():
        _text(handoff[field], f"handoff {field}")
    _sequence(handoff["affected"], "affected people", 16, 1)
    for entry in handoff["affected"]:
        _object(entry, "name role can_amend", "affected person or group")
        _text(entry["name"], "affected group name")
        _text(entry["role"], "affected group role")
        _require(entry["can_amend"] is None or type(entry["can_amend"]) is bool,
                 "Record ability to amend as true, false or null when unknown.")
    gate_ids = []
    for field in ("authority_check", "consent_check", "funding_check"):
        check = handoff[field]
        _require(check is None or isinstance(check, str) and check in checks, f"Use a check ID or null for {field}.")
        if check is not None:
            gate_ids.append(check)
    _require(len(gate_ids) == len(set(gate_ids)), "Use separate checks for authority, consent and funding.")


def validate_decision(document):
    """Validate and copy a local review plan. Null role checks are the author's scope decision."""
    _object(document, "schema title revision baseline cost_unit origin variables checks required_checks scenarios handoff", "decision review")
    _require(document["schema"] == DECISION_SCHEMA, "Use the AustinDecisionReview/1 schema.")
    _text(document["title"], "project title", 120)
    _text(document["baseline"], "project baseline")
    _text(document["cost_unit"], "objective unit", 60)
    _require(document["origin"] in ("SYNTHETIC", "OBSERVED"), "Label the project SYNTHETIC or OBSERVED.")
    revision = document["revision"]
    _require(type(revision) is int and 1 <= revision <= 2**31 - 1, "Use a positive project revision.")
    choices = _index(document["variables"], "stages", MAX_VARIABLES, 1)
    checks = _index(document["checks"], "checks", MAX_CHECKS)
    scenarios = _index(document["scenarios"], "scenarios", MAX_SCENARIOS, 1)
    for key, check in checks.items():
        _validate_check(key, check, checks, revision)
    for key, stage in choices.items():
        _validate_stage(key, stage, choices, checks)
    names = {stage["name"].casefold() for stage in choices.values()}
    _require(len(names) == len(choices), "Give each stage a unique name.")
    for key, scenario in scenarios.items():
        _validate_scenario(key, scenario, choices, checks)
    _references(document["required_checks"], checks, "project required checks")
    _validate_handoff(document["handoff"], checks)
    _dependency_order(checks, "evidence dependencies")
    _dependency_order(choices, "stage dependencies")
    _require(len(_canonical(document)) <= MAX_DECISION_BYTES, "Decision review exceeds its JSON size bound.")
    return deepcopy(document)


def decision_digest(document):
    """Pin the complete validated input, including explanatory context and evidence."""
    return _digest(validate_decision(document))


def _check_results(document):
    checks = {check["id"]: check for check in document["checks"]}
    results = {}
    for key in _dependency_order(checks, "evidence dependencies"):
        check = checks[key]
        status = check["status"]
        reason = f"{check['label']} is recorded as {status}."
        if check["revision"] != document["revision"]:
            status = "UNKNOWN"
            reason = f"{check['label']} needs evidence for project revision {document['revision']}."
        elif key == "author-cost" and document["cost_unit"] == UNASSIGNED_COST_UNIT:
            status = "UNKNOWN"
            reason = "Define an objective unit and replace placeholder costs before checking cost evidence."
        elif key == "author-rules" and not any(scenario["rules"] for scenario in document["scenarios"]):
            status = "UNKNOWN"
            reason = "Author at least one scenario rule and document its source before checking the rule set."
        elif status == "PASS":
            for prerequisite in check["depends_on"]:
                parent = results[prerequisite]
                if parent["effective_status"] != "PASS":
                    status = "UNKNOWN"
                    reason = f"{check['label']} needs {parent['label']} to pass and then needs a fresh check."
                    break
                if check["basis"].get(prerequisite) != parent["evidence_sha256"]:
                    status = "UNKNOWN"
                    reason = f"{check['label']} needs a fresh check after {parent['label']} changed."
                    break
        results[key] = {**check, "effective_status": status, "reason": reason, "evidence_sha256": _digest(check)}
    return results


def record_check(document, check_id, *, status, reference):
    """Record a new assessment, binding a pass to the current checked prerequisites."""
    document = validate_decision(document)
    record = next((entry for entry in document["checks"] if entry["id"] == check_id), None)
    _require(record is not None, f"Unknown evidence check {check_id}.")
    _require(status in EVIDENCE_STATUSES, "Choose PASS, FAIL or UNKNOWN.")
    _text(reference, "evidence reference")
    prerequisites = _check_results(document)
    basis = {}
    if status == "PASS":
        for key in record["depends_on"]:
            entry = prerequisites[key]
            _require(entry["effective_status"] == "PASS", f"Check {entry['label']} before recording this pass.")
            basis[key] = entry["evidence_sha256"]
    record.update(status=status, reference=reference, revision=document["revision"],
                  assessment=record["assessment"] + 1, basis=basis)
    return validate_decision(document)


def revise_decision(previous, draft):
    """Revise a layout while retaining or neutralizing existing observations."""
    previous = validate_decision(previous)
    draft = validate_decision(draft)
    old_checks = {entry["id"]: entry for entry in previous["checks"]}
    draft["revision"] = previous["revision"] + 1
    for entry in draft["checks"]:
        prior = old_checks.get(entry["id"])
        if prior is None:
            entry.update(status="UNKNOWN", reference="Awaiting evidence for the revised project.",
                         revision=draft["revision"], assessment=1, basis={})
        else:
            entry.update(status=prior["status"], reference=prior["reference"], revision=prior["revision"],
                         assessment=prior["assessment"], basis={key: digest for key, digest in prior["basis"].items()
                                                             if key in entry["depends_on"]})
    return validate_decision(draft)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Duplicate decision review field {key}.")
        result[key] = value
    return result


def parse_decision_document(raw):
    """Parse a bounded local JSON document and reject duplicate keys."""
    _require(isinstance(raw, (str, bytes)), "Provide decision review JSON as text or bytes.")
    try:
        raw = raw.encode("utf-8") if isinstance(raw, str) else raw
        _require(len(raw) <= MAX_DECISION_BYTES, "Decision review JSON exceeds the byte limit.")
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid decision review JSON.") from exc
    return validate_decision(document)


def load_decision_document(path):
    """Read an explicitly selected local decision JSON file."""
    with _read_regular(Path(path), MAX_DECISION_BYTES, "decision input") as (stream, _):
        return parse_decision_document(stream.read(MAX_DECISION_BYTES + 1))


def _holds(check_ids, checks):
    return [{"check_id": key, "owner": checks[key]["owner"], "status": checks[key]["effective_status"],
             "reason": checks[key]["reason"], "reference": checks[key]["reference"]}
            for key in dict.fromkeys(check_ids) if checks[key]["effective_status"] != "PASS"]


def _stage_holds(document, checks):
    stages = {stage["id"]: stage for stage in document["variables"]}
    holds = {}
    for key in _dependency_order(stages, "stage dependencies"):
        ids = list(stages[key]["checks"])
        ids.extend(item["check_id"] for parent in stages[key]["depends_on"] for item in holds[parent])
        holds[key] = _holds(ids, checks)
    return holds


def _rules(document, scenarios):
    common = [{"id": f"{entry['id']}/{key}", "reason": f"{entry['name']} requires {key}.",
               "source": f"stage:{entry['id']}.depends_on", "literals": [f"-{entry['id']}", key],
               "scenario_id": "dependencies"}
              for entry in document["variables"] for key in entry["depends_on"]]
    return common + [{**rule, "scenario_id": scenario["id"]} for scenario in scenarios for rule in scenario["rules"]]


def _all_checks(document, scenarios):
    handoff = document["handoff"]
    return [*document["required_checks"], *(handoff[k] for k in ("authority_check", "consent_check", "funding_check")
                                              if handoff[k] is not None),
            *(key for scenario in scenarios for key in scenario["checks"])]


def _problem(document, rules, excluded):
    positions = {entry["id"]: index for index, entry in enumerate(document["variables"], 1)}
    clauses = []
    for rule in rules:
        signed = tuple(sorted({-positions[literal[1:]] if literal.startswith("-") else positions[literal]
                               for literal in rule["literals"]}))
        if not any(-literal in signed for literal in signed):
            clauses.append(signed)
    clauses.extend((-positions[key],) for key in excluded)
    unique = sorted(set(clauses), key=lambda clause: (len(clause), clause))
    reduced = []
    for clause in unique:
        if not any(set(prior).issubset(clause) for prior in reduced):
            reduced.append(clause)
    return {"name": document["title"], "variables": [{"name": row["name"], "cost": row["cost"]}
                                                      for row in document["variables"]], "clauses": [list(clause) for clause in reduced]}


def compile_scenario(document, scenario_id=None):
    """Show the compiled Boolean problem and all review holds for a scenario or intersection."""
    document = validate_decision(document)
    scenarios = [entry for entry in document["scenarios"] if scenario_id is None or entry["id"] == scenario_id]
    _require(bool(scenarios), "Unknown scenario ID.")
    results = _check_results(document)
    blocked = _stage_holds(document, results)
    problem = _problem(document, _rules(document, scenarios), [key for key, value in blocked.items() if value])
    return {"problem": problem, "solver_compatible": len(problem["clauses"]) <= MAX_CLAUSES,
            "holds": _required_holds(document, scenarios, results), "stage_holds": blocked}


def _rule_failure(rule, active):
    return not any(literal[1:] not in active if literal.startswith("-") else literal in active
                   for literal in rule["literals"])


def _failures(rules, active):
    return [{"scenario_id": rule["scenario_id"], "rule_id": rule["id"], "reason": rule["reason"], "source": rule["source"]}
            for rule in rules if _rule_failure(rule, active)]


def _reference_rows(document, rules, stage_holds):
    """Independently check named constraints by enumeration, without numeric clause masks."""
    variables = document["variables"]
    for flags in product((False, True), repeat=len(variables)):
        selected = [stage["id"] for flag, stage in zip(flags, variables) if flag]
        active = set(selected)
        cost = sum(stage["cost"] for flag, stage in zip(flags, variables) if flag)
        bits = "".join("1" if flag else "0" for flag in reversed(flags))
        pending = list({hold["check_id"]: hold for key in selected for hold in stage_holds[key]}.values())
        yield {"bits": bits, "selected": selected, "cost": cost}, _failures(rules, active), pending


def _wide_minimum(problem):
    """Evaluate the compiled CNF with bitmasks when the legacy solver's clause limit is exceeded."""
    variables = problem["variables"]
    width = len(variables)
    masks = []
    for clause in problem["clauses"]:
        positive = sum(1 << (literal - 1) for literal in clause if literal > 0)
        negative = sum(1 << (-literal - 1) for literal in clause if literal < 0)
        masks.append((positive, negative))
    costs = [item["cost"] for item in variables]
    feasible = []
    for candidate in range(1 << width):
        if all(candidate & positive or ~candidate & negative for positive, negative in masks):
            value = sum(cost for index, cost in enumerate(costs) if candidate & (1 << index))
            feasible.append((value, format(candidate, f"0{width}b")))
    best = min((value for value, _ in feasible), default=None)
    return {"cost": best, "ties": [bits for value, bits in feasible if value == best], "feasible_count": len(feasible)}


def _result(status, assignments=0, *, cost=None, selected=None, ties=None, count=0):
    return {"status": status, "cost": cost, "selected": selected or [], "ties": ties or [],
            "feasible_count": count, "assignments_checked": assignments, "cheaper_exclusions": [],
            "partial_best": None, "method": None, "constraint_feasible_count": None, "exclusion_checks": []}


def _required_holds(document, scenarios, checks):
    holds = _holds(_all_checks(document, scenarios), checks)
    if document["cost_unit"] == UNASSIGNED_COST_UNIT and not any(ballot["check_id"] == "author-cost" for ballot in holds):
        holds.append({"check_id": "author-cost", "owner": document["handoff"]["decision_owner"], "status": "UNKNOWN",
                      "reason": "Define an objective unit and replace placeholder costs before searching.",
                      "reference": "project:cost_unit"})
    return holds


def _search(document, scenarios, checks, stage_holds):
    holds = _required_holds(document, scenarios, checks)
    if holds:
        return _result("HELD"), holds
    rules = _rules(document, scenarios)
    excluded = [key for key, value in stage_holds.items() if value]
    problem = _problem(document, rules, excluded)
    if len(problem["clauses"]) <= MAX_CLAUSES:
        solution = exact_minimum(problem)
        method = "exact_minimum"
    else:
        solution = _wide_minimum(problem)
        method = "scenario_intersection"
    assignments = list(_reference_rows(document, rules, stage_holds))
    qualified = [row for row, failures, pending in assignments if not failures and not pending]
    best = min((row["cost"] for row in qualified), default=None)
    ties = sorted((row for row in qualified if row["cost"] == best), key=lambda row: row["bits"])
    if solution["cost"] != best or solution["ties"] != [row["bits"] for row in ties] or solution["feasible_count"] != len(qualified):
        raise RuntimeError("Independent decision enumeration disagrees with the compiled result.")
    viable = [(row, pending) for row, failures, pending in assignments if not failures and pending]
    pending = [(row, evidence) for row, evidence in viable if (best is None or row["cost"] <= best)
               and all(hold["status"] != "FAIL" for hold in evidence)]
    status = "HELD" if pending else "OPTIMUM" if ties else "INFEASIBLE"
    result = _result(status, len(assignments), cost=best if status == "OPTIMUM" else None,
                     selected=ties[0]["selected"] if status == "OPTIMUM" else None,
                     ties=ties if status == "OPTIMUM" else None, count=len(qualified))
    result["method"] = method
    result["constraint_feasible_count"] = len(qualified) + len(viable)
    if status == "HELD" and ties:
        result["partial_best"] = ties[0]
    if status == "INFEASIBLE":
        failed_ids = [hold["check_id"] for _, evidence in viable for hold in evidence if hold["status"] == "FAIL"]
        result["exclusion_checks"] = _holds(failed_ids, checks)
    if status == "OPTIMUM":
        result["cheaper_exclusions"] = [
            {**row, "failures": failures[:1], "holds": evidence[:1],
             "failing_scenarios": list(dict.fromkeys(item["scenario_id"] for item in failures)),
             "failure_count": len(failures), "hold_count": len(evidence)}
            for row, failures, evidence in sorted(assignments, key=lambda entry: (entry[0]["cost"], entry[0]["bits"]))
            if row["cost"] < best
        ]
    pending_checks = [hold["check_id"] for _, evidence in pending for hold in evidence]
    return result, _holds(pending_checks, checks)


def review_decision(document):
    """Compare individual scenarios and independently verify the exact shared choice."""
    document = validate_decision(document)
    all_checks = _check_results(document)
    transitive_holds = _stage_holds(document, all_checks)
    comparisons = []
    for scenario in document["scenarios"]:
        result, holds = _search(document, [scenario], all_checks, transitive_holds)
        comparisons.append({"id": scenario["id"], "title": scenario["title"], "status": result["status"],
                            "result": result, "holds": holds})
    result, holds = _search(document, document["scenarios"], all_checks, transitive_holds)
    witnesses = []
    if result["status"] == "OPTIMUM":
        witnesses.append((None, result["ties"][0]))
    elif result["status"] == "INFEASIBLE":
        witnesses.extend((entry["id"], entry["result"]["ties"][0]) for entry in comparisons if entry["status"] == "OPTIMUM")
    scenario_matrix = []
    for source_id, witness in witnesses:
        outcomes = []
        for scenario in document["scenarios"]:
            failures = _failures(_rules(document, [scenario]), set(witness["selected"]))
            scenario_holds = _required_holds(document, [scenario], all_checks)
            outcomes.append({"scenario_id": scenario["id"], "status": "HELD" if scenario_holds else "FAIL" if failures else "PASS",
                             "failures": failures, "holds": scenario_holds})
        scenario_matrix.append({"source_scenario_id": source_id, "candidate": witness, "outcomes": outcomes})
    return {"schema": REPORT_SCHEMA, "input_sha256": _digest(document), "title": document["title"],
            "revision": document["revision"], "cost_unit": document["cost_unit"], "origin": document["origin"],
            "status": result["status"], "result": result, "holds": holds, "scenarios": comparisons,
            "scenario_matrix": scenario_matrix,
            "checks": [all_checks[row["id"]] for row in document["checks"]],
            "stages": [{**row, "holds": transitive_holds[row["id"]]} for row in document["variables"]]}


def verify_decision(document, report):
    """Replay gates and both search evaluators. A digest checks contents, not external authority."""
    expected = review_decision(document)
    try:
        matches = _canonical(report) == _canonical(expected)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Decision result must be valid JSON.") from exc
    _require(matches, "Decision result does not match independent replay.")
    return expected


def offline_service_example():
    """Create a worked synthetic public-office decision. No operational approval is implied."""
    interface_owner = "Fictional service coordinator"

    def stage(key, name, cost, change, interface, tolerance, *, after=(), checks=(),
              stop="Pause if the promised handoff or privacy boundary fails.",
              recovery="Preserve open case IDs, reassign unresolved cases, and recheck ownership."):
        return {"id": key, "name": name, "cost": cost, "depends_on": list(after), "checks": list(checks),
                "starting_state": "Five fictional service requests before rollout.", "change": change,
                "expected": tolerance, "stop": stop, "recovery": recovery,
                "interfaces": [{"reference": interface, "tolerance": tolerance, "owner": interface_owner}]}

    stages = [
        stage("paper", "Paper intake", 4, "Record requests on serialized forms.", "Paper form to intake roster",
              "Each case has one serial, one owner, and a recorded disposition.", checks=("paper-practice",)),
        stage("local", "Offline device intake", 6, "Use the local queue without network sign-in.", "Offline queue to case roster",
              "Each durable case ID survives restart and reconciles once.", after=("spare",), checks=("offline-practice",)),
        stage("spare", "Spare device", 3, "Hold a separately prepared device.", "Ready spare to local queue",
              "A replacement can recover the same case IDs.", checks=("restore-test",)),
        stage("escalation", "Independent escalation", 2, "Route urgent cases through the approved offline contact.",
              "Front desk to urgent-case owner", "One named recipient can accept each urgent request."),
        stage("reconcile", "Reconciliation", 3, "Compare captured IDs with the authoritative queue after recovery.",
              "Intake roster to restored queue", "All five approved case IDs are resolved exactly once."),
        stage("cloud", "Cloud intake", 1, "Use the normal online intake route.", "Staff sign-in to intake queue",
              "Cloud identity and intake are available for the named scenario."),
    ]

    def check(key, label, *, depends_on=()):
        return {"id": key, "label": label, "owner": interface_owner, "status": "UNKNOWN",
                "reference": "Synthetic case starts without an observation.", "revision": 1, "assessment": 1,
                "depends_on": list(depends_on), "basis": {}}

    checks = [
        check("authority", "Authority to run the tabletop"), check("consent", "Participation and use of fictional records"),
        check("funding", "Staff time and materials available for tabletop"),
        check("paper-practice", "Paper roster and handoff exercise"),
        check("restore-test", "Prepared spare and restore evidence"),
        check("offline-practice", "Offline queue and reconciliation exercise", depends_on=("restore-test",)),
        check("recovery-check", "Verified reconciliation procedure", depends_on=("paper-practice",)),
    ]

    def rule(key, literals, reason):
        return {"id": key, "literals": literals.split(), "reason": reason, "source": "Synthetic public-office drill protocol"}

    service = [rule("intake-needed", "paper local cloud", "Choose an intake route."),
               rule("paper-or-device", "-paper -local", "Allocate staffing to only one intake route."),
               rule("paper-or-cloud", "-paper -cloud", "Allocate staffing to only one intake route."),
               rule("device-or-cloud", "-local -cloud", "Allocate staffing to only one intake route."),
               rule("reconcile", "reconcile", "Preserve stable IDs and reconcile the records exactly once.")]
    scenarios = [{"id": "normal", "title": "Normal office intake", "rules": service, "checks": []}]
    for key, title, reason, required in (
        ("outage", "Email and sign-in outage", "Cloud intake shares the failed identity route.", []),
        ("recovery", "Recovery with intake still isolated", "Cloud intake stays isolated during reconciliation.", ["recovery-check"]),
    ):
        scenarios.append({"id": key, "title": title, "checks": required, "rules": [
            *service, rule("isolate-cloud", "-cloud", reason),
            rule("offline-escalation", "escalation", "The urgent-case route must work during isolation.")]})
    document = {"schema": DECISION_SCHEMA, "title": "Public office intake continuity", "revision": 1,
                "baseline": "Tabletop of five fictional cases. Email and identity may fail together.",
                "cost_unit": "illustrative effort points", "origin": "SYNTHETIC", "variables": stages, "checks": checks,
                "required_checks": [], "scenarios": scenarios, "handoff": {
                    "affected": [{"name": "Fictional service requesters", "role": "Receive status and can flag missing or duplicate cases", "can_amend": False},
                                 {"name": "Fictional intake and duty leads", "role": "Review escalation and amend the tabletop procedure", "can_amend": True}],
                    "decision_owner": "Fictional duty lead", "delivery_owner": "Fictional intake lead",
                    "verifier": "Fictional reviewer", "resources": "Tabletop staff and mock cases only. Values are illustrative effort points.",
                    "authority_check": "authority", "consent_check": "consent", "funding_check": "funding",
                }}
    for entry in checks:
        key = entry["id"]
        document = record_check(document, key, status="PASS", reference=f"Synthetic assumption for tabletop only/{key}")
    return document
