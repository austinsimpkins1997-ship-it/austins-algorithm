"""Portable, bounded evidence examples for the Future Reader controller."""

import base64
from fractions import Fraction
from hashlib import sha256
from math import comb, sqrt
import re
from statistics import median

from .controller import critical_path

C_M_PER_S = 299_792_458
OPERATIONS = ("PINPOINT", "TRAVEL", "REVERSE", "CERTIFY")
CASES = ("cern", "cenomanian", "archive", "supplied-studies")
ARCHIVE_SHARD_VERSION = "xor-2-of-3/v1"
MAX_ARCHIVE_PAYLOAD = 1_048_576


def _source(id_, title, url):
    return {"id": id_, "title": title, "url": url}


SI = _source("bipm-si", "BIPM definition of the metre", "https://www.bipm.org/en/si-base-units/metre")
ICS = _source("ics-chart-2026-06", "International Chronostratigraphic Chart v2026/06",
              "https://stratigraphy.org/chart/?scale=linear")
REGIONAL = _source("nps-dinosaur-freeway", "National Park Service, A Dinosaur Freeway",
                   "https://www.nps.gov/subjects/fossils/dinosaur-freeway.htm")
ASH = _source("nps-volcanoes-fossils", "National Park Service, Volcanoes and Fossils",
              "https://www.nps.gov/subjects/fossils/volcanoes-and-fossils.htm")
SCHEDULE = _source("cern-2024-schedule", "CERN accelerator schedule, October 4, 2024",
                   "https://home.cern/updated-schedule-cerns-accelerators/")
LAST_BEAMS = _source("cern-2026-last-beams", "CERN operations report, June 29, 2026",
                     "https://home.cern/so-long-and-thanks-for-all-the-collisions-2/")
SHUTDOWN = _source("cern-2026-ls3", "CERN Long Shutdown 3 announcement, June 29, 2026",
                   "https://home.cern/cern-bids-farewell-to-the-lhc-and-enters-long-shutdown-3/")
GAMMA = _source("alvager-1964", "Alväger et al., Physics Letters 12, 260–262",
                "https://www.sciencedirect.com/science/article/abs/pii/0031916364910959")
MUON = _source("bailey-1977", "Bailey et al., CERN muon lifetime experiment",
               "https://www.nature.com/articles/268301a0")
OPERA = _source("opera-2012", "OPERA Collaboration, corrected timing measurement",
                "https://arxiv.org/abs/1109.4897v4")
OPERA_FOLLOWUP = _source("opera-dedicated-2012", "OPERA Collaboration, dedicated 2012 beam data",
                         "https://arxiv.org/abs/1212.1276v2")
OPERA_TIMING = _source("cern-opera-timing", "CERN OPERA update, June 8, 2012",
                       "https://home.cern/opera-experiment-reports-anomaly-in-flight-time-of-neutrinos-from-cern-to-gran-sasso/")
STRUCTURED_LIGHT = _source("giovannini-2015", "Giovannini et al., spatially structured light",
                           "https://arxiv.org/abs/1411.3987")
VACUUM = _source("cern-vacuum", "CERN descriptions of the LHC vacuum systems",
                 "https://home.cern/science/engineering/vacuum-empty-interstellar-space")
MACHINE = _source("cern-machine", "CERN LHC Machine design report, 2008",
                  "https://cds.cern.ch/record/1129806/files/jinst8_08_s08001.pdf")
DESIGN = _source("cern-hilumi-design", "CERN HL-LHC Technical Design Report, CERN-2020-010",
                 "https://e-publishing.cern.ch/index.php/CYRM/article/download/1188/986/4985")
NAL = _source("nal-1968-design", "National Accelerator Laboratory design report, 1968",
              "https://lss.fnal.gov/archive/design/fermilab-design-1968-01.pdf")
RAPID_UTC = _source("bipm-rapid-utc", "BIPM Rapid UTC files 2624–2628",
                    "https://webtai.bipm.org/ftp/pub/tai/Rapid-UTC/utcr/")
CIRCULAR_T = (
    _source("bipm-circular-t-462", "BIPM Circular T 462",
            "https://webtai.bipm.org/ftp/pub/tai/Circular-T/cirt/cirt.462"),
    _source("bipm-circular-t-463", "BIPM Circular T 463",
            "https://webtai.bipm.org/ftp/pub/tai/Circular-T/cirt/cirt.463"),
)
BRIEF = _source("supplied-brief", "User-supplied master prompt, sections 8–10", "../MASTER_PROMPT.txt")


def timing_equivalence(path_m="11.42", speed_fraction="0.996"):
    """Return the exact model result with decimal display values in SI-derived units."""
    length, ratio = Fraction(path_m), Fraction(speed_fraction)
    if length <= 0 or not 0 < ratio <= 1:
        raise ValueError("Expected a positive path and a speed fraction in (0, 1].")
    reference_time = length / C_M_PER_S
    slower_time = reference_time / ratio
    extra_path = length * (1 / ratio - 1)
    delay = slower_time - reference_time
    if (length + extra_path) / C_M_PER_S != slower_time:
        raise ArithmeticError("Equivalent paths disagree.")
    return {
        "path_m": str(path_m),
        "hypothetical_speed_over_c": str(speed_fraction),
        "reference_c_m_per_s": C_M_PER_S,
        "reference_time_ns": round(float(reference_time * 10**9), 9),
        "hypothetical_time_ns": round(float(slower_time * 10**9), 9),
        "delay_ps": round(float(delay * 10**12), 9),
        "equivalent_extra_path_cm": round(float(extra_path * 100), 9),
        "delay_s_exact": str(delay),
        "extra_path_m_exact": str(extra_path),
        "method": "t = L/v, extra_path = L * (1/r - 1), r = v/c",
    }


def age_membership(target_ma, younger_ma, older_ma):
    """Compare an age against two inclusive boundary ranges in Ma before present."""
    target = Fraction(target_ma)
    younger = tuple(map(Fraction, younger_ma))
    older = tuple(map(Fraction, older_ma))
    if len(younger) != 2 or len(older) != 2 or not 0 <= younger[0] <= younger[1] <= older[0] <= older[1]:
        raise ValueError("Expected ordered, disjoint age ranges.")
    if younger[1] < target < older[0]:
        return "INSIDE_FOR_ALL_BOUNDS"
    if target < younger[0] or target > older[1]:
        return "OUTSIDE_FOR_ALL_BOUNDS"
    return "BOUNDARY_AMBIGUOUS"


def retention_probability(population, retained, decisive):
    """Exact chance of at least one decisive record in uniform sampling without replacement."""
    if any(type(value) is not int for value in (population, retained, decisive)):
        raise ValueError("Archive counts must be integers.")
    if not 1 <= population <= 1_000_000 or not 0 <= retained <= population or not 0 <= decisive <= population:
        raise ValueError("Archive counts fall outside the bounded model.")
    if retained + decisive > population:
        return Fraction(1)
    if min(retained, decisive) > 10_000:
        raise ValueError("Archive calculation exceeds the combination budget.")
    # Use the smaller count to keep exact arithmetic affordable.
    if decisive < retained:
        retained, decisive = decisive, retained
    return 1 - Fraction(comb(population - decisive, retained), comb(population, retained))


def _uniform_retention_hits(seed, trials):
    """Stable, implementation-independent draw using SHA-256 and Floyd sampling."""
    population, retained, decisive = 10_000, 10, 6
    counter = 0
    hits = 0
    for _ in range(trials):
        chosen = set()
        for j in range(population - retained, population):
            bound = j + 1
            ceiling = 2**64 - (2**64 % bound)
            while True:
                draw = int.from_bytes(sha256(f"future-reader/archive/v1/{seed}/{counter}".encode("ascii")).digest()[:8],
                                      "big")
                counter += 1
                if draw < ceiling:
                    break
            draw %= bound
            chosen.add(j if draw in chosen else draw)
        hits += any(record < decisive for record in chosen)
    return hits


def _wilson_95(hits, trials):
    z = 1.959963984540054
    p = hits / trials
    denom = 1 + z*z/trials
    center = (p + z*z/(2*trials))/denom
    margin = z * sqrt(p*(1-p)/trials + z*z/(4*trials*trials)) / denom
    return [round(center - margin, 9), round(center + margin, 9)]


def archive_experiment(seed=20261113, trials=10_000):
    if type(seed) is not int or not 0 <= seed < 2**64 or type(trials) is not int or not 1 <= trials <= 20_000:
        raise ValueError("Expected a nonnegative 64-bit seed and 1–20,000 trials.")
    probability = retention_probability(10_000, 10, 6)
    hits = _uniform_retention_hits(seed, trials)
    return {
        "evidence_kind": "SIMULATION",
        "seed": seed,
        "trials": trials,
        "population": 10_000,
        "records_retained_per_trial": 10,
        "decisive_records": 6,
        "recoveries": hits,
        "frequency": hits / trials,
        "wilson_95_interval": _wilson_95(hits, trials),
        "analytic_probability": float(probability),
        "analytic_probability_exact": str(probability),
        "model_version": "future-reader/archive/v1",
        "sampler": "Uniform Floyd sampling per trial. Draw SHA-256 of ASCII model/seed/counter, take first eight bytes as a big-endian integer and use rejection before taking a remainder.",
    }


def archive_recovery(*, raw_record, summary, frame, calibration, trusted_key):
    """Score recovery outcomes independently for the synthetic regional claim."""
    if any(type(value) is not bool for value in (raw_record, summary, frame, calibration, trusted_key)):
        raise ValueError("Survival flags must be booleans.")
    if not (raw_record or summary):
        meaning = "UNRECOVERABLE"
    elif not frame:
        meaning = "AMBIGUOUS"
    elif raw_record:
        meaning = "BOUNDED"
    else:
        meaning = "BOUNDED_SUMMARY"
    return {
        "decisive_original_bytes_available": raw_record,
        "claim_meaning": meaning,
        "original_provenance": "CHECKABLE" if raw_record and trusted_key else "UNRESOLVED",
        "underlying_evidence": "AVAILABLE" if raw_record and calibration and frame else "INCOMPLETE",
        "local_flooding": "AMBIGUOUS" if frame and (raw_record or summary) else "UNRECOVERABLE",
    }


def _xor(left, right):
    return bytes(a ^ b for a, b in zip(left, right))


def split_archive(payload):
    """Split a bounded byte string into two data shards and one XOR parity shard."""
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_ARCHIVE_PAYLOAD:
        raise ValueError("Expected 1 to 1,048,576 archive bytes.")
    width = (len(payload) + 1) // 2
    left, right = payload[:width], payload[width:].ljust(width, b"\0")
    cohort = sha256(payload).hexdigest()
    return [
        {"schema": ARCHIVE_SHARD_VERSION, "index": index, "original_size": len(payload),
         "original_sha256": cohort, "shard_sha256": sha256(piece).hexdigest(),
         "data_base64": base64.b64encode(piece).decode("ascii")}
        for index, piece in enumerate((left, right, _xor(left, right)))
    ]


def restore_archive(shards, *, expected_sha256):
    """Recover bytes after one shard erasure, using a separately trusted payload digest."""
    if type(shards) not in (list, tuple) or not 2 <= len(shards) <= 3:
        raise ValueError("Exactly two or three archive shards are required.")
    if type(expected_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("A trusted SHA-256 digest of the original bytes is required.")
    expected_fields = {"schema", "index", "original_size", "original_sha256", "shard_sha256", "data_base64"}
    pieces = {}
    size = None
    for shard in shards:
        if type(shard) is not dict or set(shard) != expected_fields or shard["schema"] != ARCHIVE_SHARD_VERSION:
            raise ValueError("Unsupported archive shard schema.")
        if shard["original_sha256"] != expected_sha256:
            raise ValueError("Shard does not match the expected original digest.")
        index, declared_size = shard["index"], shard["original_size"]
        if type(index) is not int or not 0 <= index <= 2 or index in pieces:
            raise ValueError("Invalid or duplicate shard index.")
        if type(declared_size) is not int or not 1 <= declared_size <= MAX_ARCHIVE_PAYLOAD or size not in (None, declared_size):
            raise ValueError("Invalid or conflicting original archive size.")
        size = declared_size
        width = (size + 1) // 2
        encoded = shard["data_base64"]
        if type(encoded) is not str or len(encoded) != 4 * ((width + 2) // 3):
            raise ValueError("Shard data size disagrees with the declared archive size.")
        try:
            piece = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError("Invalid Base64 archive shard.") from error
        if len(piece) != width or sha256(piece).hexdigest() != shard["shard_sha256"]:
            raise ValueError("Archive shard integrity check failed.")
        if base64.b64encode(piece).decode("ascii") != encoded:
            raise ValueError("Archive shard Base64 is not canonical.")
        pieces[index] = piece
    if 0 not in pieces:
        pieces[0] = _xor(pieces[1], pieces[2])
    if 1 not in pieces:
        pieces[1] = _xor(pieces[0], pieces[2])
    if 2 in pieces and _xor(pieces[0], pieces[1]) != pieces[2]:
        raise ValueError("Archive parity check failed.")
    joined = pieces[0] + pieces[1]
    if any(joined[size:]):
        raise ValueError("Archive padding must be zero.")
    restored = joined[:size]
    if sha256(restored).hexdigest() != expected_sha256:
        raise ValueError("Restored archive bytes disagree with the trusted digest.")
    return restored


def _reply(case, operation, status, summary, details, sources, limits, next_check):
    return {"schema_version": 1, "case": case, "operation": operation, "status": status,
            "summary": summary, "details": details, "sources": [dict(source) for source in sources],
            "limits": limits, "next_check": next_check}


def cern_window(*, approval_s=20, queue_s=10, prerequisites_ready_s=0, calibration_expires_s=None,
                clock_uncertainty_s=0):
    """Simulate a deadline and calibration validity in the hypothetical CERN workflow."""
    stages = [
        {"id": "approval", "duration": approval_s, "requires": []},
        {"id": "survey", "duration": 40, "requires": ["approval"]},
        {"id": "calibration", "duration": 45, "requires": ["approval"]},
        {"id": "queue", "duration": queue_s, "requires": ["survey", "calibration"]},
        {"id": "measurement", "duration": 15, "requires": ["queue"]},
        {"id": "verification", "duration": 15, "requires": ["measurement"]},
    ]
    path = critical_path(stages, deadline=120, margin=10, clock_uncertainty=clock_uncertainty_s)
    if type(prerequisites_ready_s) is not int or prerequisites_ready_s < 0:
        raise ValueError("Prerequisite arrival must be a nonnegative whole second.")
    if calibration_expires_s is not None and (type(calibration_expires_s) is not int or calibration_expires_s < 0):
        raise ValueError("Calibration expiry must be a nonnegative whole second.")
    latest = path["latest_safe_start"]
    measurement_complete = prerequisites_ready_s + approval_s + 45 + queue_s + 15 + clock_uncertainty_s
    if calibration_expires_s is not None:
        latest = min(latest, prerequisites_ready_s + calibration_expires_s - measurement_complete)
    open_ = prerequisites_ready_s <= latest
    return {
        "clock": "Relative seconds from opening of approved setup",
        "stages": stages,
        "deadline_s": path["deadline"],
        "required_safety_margin_s": path["safety_margin"],
        "critical_path_s": path["critical_path_bound"],
        "critical_path": path["critical_path"],
        "latest_feasible_start_s": path["latest_feasible_start"],
        "deadline_safe_start_s": path["latest_safe_start"],
        "latest_safe_start_s": latest,
        "equality_qualifies": path["equality"],
        "clock_uncertainty_s": clock_uncertainty_s,
        "prerequisites_ready_s": prerequisites_ready_s,
        "calibration_expires_s": calibration_expires_s if calibration_expires_s is not None else "NO_EXPIRY_MODELED",
        "earliest_measurement_completion_s": measurement_complete,
        "status": "SYNTHETIC_WINDOW_OPEN" if open_ else "SYNTHETIC_FALLBACK_REQUIRED",
        "next_action": "Prepare and verify prerequisites." if open_ else "Recalibrate within the remaining window or wait for another opening.",
    }


def _cern(operation):
    question = "Independently survey the path and calibrate both timing endpoints, including shared references."
    limits = [
        "The 11.42 m path is a supplied modeled median. The 0.996c speed is hypothetical.",
        "The first wall hit and the circumference of the accelerator describe different paths.",
        "These alternatives match flight time in the simplified model. Actual speed, path and calibration are unresolved.",
    ]
    if operation == "PINPOINT":
        return _reply("cern", operation, "AMBIGUOUS",
                      "The same modeled arrival time fits a slower flight or a longer path at c.",
                      {"candidates": ["11.42 m at hypothetical 0.996c", "11.42 m plus about 4.6 cm at c"],
                       "model": timing_equivalence(), "observed_modern_lhc_photon_speed": "UNKNOWN"},
                      [SI, MACHINE, DESIGN, BRIEF], limits, question)
    if operation == "TRAVEL":
        return _reply("cern", operation, "CONDITIONAL",
                      "A path survey can distinguish these two models if its uncertainty is small enough and its failure sources are independent.",
                      {"options": [
                          {"action": "Independent geometric survey", "resolves": "Proportional path error", "conditions": "Document reference frame and endpoint alignment. Aim for total path uncertainty well below 4.6 cm."},
                          {"action": "Calibrated time of flight", "resolves": "Fixed electronics offset versus distance-dependent slope", "conditions": "Synchronize both endpoints independently and quantify uncertainty well below 153 ps over the modeled baseline."},
                          {"action": "Separate clock-rate comparison", "resolves": "A clock-rate hypothesis", "conditions": "Use independent clocks and a cadence matched to the proposed effect."},
                          {"action": "Prepare or wait", "resolves": "A missing prerequisite or expiring calibration", "conditions": "Reserve reversible setup before the decision cutoff. Hold any commitment with unverified prerequisites."},
                      ], "shared_failure_sources": ["Clock reference", "Survey calibration", "Endpoint alignment", "Shared trigger"],
                       "hypothetical_window": cern_window(),
                       "sensitivity": {
                           "approval_takes_26_s": cern_window(approval_s=26),
                           "detector_queue_is_15_s": cern_window(queue_s=15),
                           "evidence_arrives_at_6_s": cern_window(prerequisites_ready_s=6),
                           "calibration_expires_at_89_s": cern_window(calibration_expires_s=89),
                           "clock_uncertainty_is_2_s": cern_window(clock_uncertainty_s=2),
                       }},
                      [SI, BRIEF], limits, question)
    if operation == "REVERSE":
        timeline = [
            {"event": "Revised schedule announced", "event_date": "2024-10-04", "available_date": "2024-10-04", "evidence_kind": "REPORTED_OBSERVATION", "source": SCHEDULE["id"]},
            {"event": "Last beams dumped", "event_date": "2026-06-27", "available_date": "2026-06-29", "evidence_kind": "REPORTED_OBSERVATION", "source": LAST_BEAMS["id"]},
            {"event": "LHC enters Long Shutdown 3", "event_date": "2026-06-29", "available_date": "2026-06-29", "evidence_kind": "REPORTED_OBSERVATION", "source": SHUTDOWN["id"]},
            {"event": "Planned start of Run 4", "event_month": "2030-06", "available_date": "2024-10-04", "evidence_kind": "FORECAST", "source": SCHEDULE["id"]},
        ]
        return _reply("cern", operation, "HISTORICAL_VIEW",
                      "The 2024 cutoff retains the schedule and Run 4 plan as then reported.",
                      {"cutoff_date": "2024-10-04", "perspective": "Public availability in the selected records",
                       "available_at_cutoff": [record for record in timeline if record["available_date"] <= "2024-10-04"],
                       "later_evidence": [record for record in timeline if record["available_date"] > "2024-10-04"],
                       "recorded_release_day_precision": "Calendar day", "external_effects": "NONE"},
                      [SCHEDULE, LAST_BEAMS, SHUTDOWN], limits, question)
    return _reply("cern", operation, "PROVED_IN_MODEL",
                  "About 153 ps and 4.6 cm are equivalent in the declared flight-time model.",
                  {"property": "Mathematical equivalence of two flight times",
                   "assumptions": ["Constant speed along each path", "Identical endpoint time reference", "Fixed SI value of c"],
                   "proof": timing_equivalence(),
                   "cross_checks": {"historical_31_450_m": timing_equivalence("31.450"),
                                    "one_kilometre": timing_equivalence("1000"),
                                    "one_centimetre_at_c_ps": round(10**10 / C_M_PER_S, 9)},
                   "invalidation": "Recompute for a changed path, speed ratio, propagation model or synchronization assumption.",
                   "context": _cern_context()},
                  [SI, GAMMA, MUON, OPERA, OPERA_FOLLOWUP, OPERA_TIMING, STRUCTURED_LIGHT, VACUUM, MACHINE,
                   DESIGN, NAL, RAPID_UTC, *CIRCULAR_T, BRIEF], limits, question)


def _cern_context():
    return {
        "published": [
            {"source": GAMMA["id"], "evidence_kind": "REPORTED_OBSERVATION", "scope": "1964 CERN Proton Synchrotron gamma ray flight measurement", "speed_km_per_s": 299770, "reported_uncertainty_km_per_s": 40, "baseline_m": "31.450", "baseline_uncertainty_m": "0.0015", "materials_reported": ["air", "beryllium", "Mylar"]},
            {"source": MUON["id"], "evidence_kind": "REPORTED_OBSERVATION", "scope": "1977 relativistic muon lifetime measurement in the CERN storage ring"},
            {"source": OPERA["id"], "evidence_kind": "REPORTED_OBSERVATION", "scope": "Corrected OPERA neutrino timing analysis"},
            {"source": OPERA_FOLLOWUP["id"], "evidence_kind": "REPORTED_OBSERVATION", "scope": "Dedicated 2012 OPERA neutrino run was compatible with zero timing discrepancy within uncertainty"},
            {"source": OPERA_TIMING["id"], "evidence_kind": "REPORTED_OBSERVATION", "scope": "CERN attributed the earlier anomaly to the fibre-optic timing system."},
            {"source": STRUCTURED_LIGHT["id"], "evidence_kind": "REPORTED_OBSERVATION", "scope": "Axial group delay of spatially structured optical beams"},
            {"source": MACHINE["id"], "evidence_kind": "SPECIFICATION", "scope": "Nominal LHC dipole length, bending radius and beam-screen opening", "dipole_m": "14.3", "radius_m": "2803.95", "opening_mm": ["44.04", "34.28"], "ring_circumference_km_approx": "26.7"},
            {"source": VACUUM["id"], "evidence_kind": "SPECIFICATION", "scope": "General beam-pipe and cryogenic vacuum context", "sector_pressure_on_shutdown": "UNKNOWN"},
            {"source": NAL["id"], "evidence_kind": "SPECIFICATION", "scope": "1968 National Accelerator Laboratory design, PDF page 81. About 99.6% of c describes 10 BeV injection protons."},
        ],
        "bipm": {
            "evidence_kind": "CALCULATION_FROM_PUBLISHED_DATA",
            "period": ["2026-06-08", "2026-07-12"],
            "estimator": "Select laboratories with data on all 35 UTC days. Compute the median of each laboratory's consecutive daily changes in UTCr−UTC(k).",
            "complete_laboratories": 59,
            "any_data_laboratories": 67,
            "changes_27_to_30_jun_ns": ["-0.1", "0.1", "-0.3"],
            "bern_wabern_27_to_30_jun_ns": ["0.8", "0.8", "0.9", "0.9"],
            "five_day_cross_check": {"series": "UTC−UTC(k) in Circular T, distinct from UTCr−UTC(k)",
                                     "bern_wabern_mjd": [61214, 61219, 61224],
                                     "bern_wabern_ns": ["0.8", "0.8", "0.8"]},
            "tai_scale_intervals": [
                {"circular": 462, "mjd": [61189, 61219], "d_in_1e_minus_15": "-0.30", "u_in_1e_minus_15": "0.09"},
                {"circular": 463, "mjd": [61219, 61249], "d_in_1e_minus_15": "-0.09", "u_in_1e_minus_15": "0.12"},
            ],
            "limit": "Daily relative offsets and monthly averages leave short-lived, site-specific, and common-mode effects unresolved.",
        },
        "supplied": {
            "status": "NOT_REPRODUCED",
            "notes": ["The supplied CMS figure is attributed to sample purity. Its original CMS citation remains unverified.",
                      "The claim of modern LHC photons at 0.996c has no verified source."],
            "trajectory_10000_gap_m": [0, 1, 2], "trajectory_10000_medians_m": ["11.10", "11.46", "11.75"],
            "trajectory_100000": {"batches": 100, "cases_per_batch": 1000,
                                  "classifications": {"emitting_bend": 22253, "gap": 6603, "next_bend": 71144},
                                  "median_path_m": "11.42", "supplied_median_arrival_times_ns": ["38.091", "38.244"],
                                  "precision_difference": "Using the literal rounded 11.42 m gives 38.0930 and 38.2460 ns. The underlying unrounded path is unavailable.",
                                  "paired_middle_90_percent_delay_ps": [142, 169],
                                  "reference_middle_90_percent_arrival_ns": ["35.42", "42.14"],
                                  "inputs": "Stratified emission point and horizontal launch angle, vertical angles and starting offsets, gaps of 0–2 m.",
                                  "missing": "Original seed, exact sampler, solver version, and per-trial records."},
            "solver_benchmark": {"reference_bisection_steps": 45, "supplied_max_path_difference_pm_lt": "0.4",
                                 "runs": 11, "supplied_reference_median_ms": "66.7", "supplied_direct_median_ms": "3.06",
                                 "missing": "Original solver and timing environment."},
            "baseline_slope_trials": {"trials_per_scenario": 10000, "baselines_m": [5, 10, 20],
                                      "per_baseline_random_uncertainty_ps": 3,
                                      "path_slope_and_speed_slope_are_equivalent_ps_per_m": "13.4",
                                      "missing": "Original noise model, seed, and classification rule."},
        },
    }


def rapid_utc_summary(issue_texts):
    """Recalculate the fixed comparison from BIPM Rapid UTC plain-text issues."""
    return _rapid_utc_summary(_parse_rapid_utc_issues(issue_texts))


def _parse_rapid_utc_issues(issue_texts):
    laboratories = {}
    target_days = set(range(61199, 61234))
    for issue in issue_texts:
        date_row = next((row for row in issue.splitlines() if re.match(r"^\s*MJD\s+\d", row)), None)
        if date_row is None:
            raise ValueError("Rapid UTC issue is missing its MJD row.")
        days = [int(value) for value in date_row.split()[1:]]
        if len(days) != 7 or len(set(days)) != 7:
            raise ValueError("Rapid UTC issue must have seven distinct days.")
        for row in issue.splitlines():
            entry = re.match(r"^([A-Z0-9]+)\s+\(.*?\)\s+(.+)", row)
            if entry is None:
                continue
            values = entry[2].split()
            if len(values) != len(days):
                raise ValueError("Rapid UTC row does not match its date columns.")
            lab = laboratories.setdefault(entry[1], {})
            for day, value in zip(days, values):
                if day not in target_days or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
                    continue
                result = Fraction(value)
                if day in lab and lab[day] != result:
                    raise ValueError("Conflicting laboratory offsets for the same day.")
                lab[day] = result
    return laboratories


def rapid_utc_extract(issue_texts):
    """Preserve source hashes, observed days and the four offsets needed for replay."""
    laboratories = _parse_rapid_utc_issues(issue_texts.values())
    return {
        "schema_version": 1,
        "scope": "BIPM Rapid UTC, 2026-06-08 through 2026-07-12. Integer offsets are tenths of ns.",
        "original_files": [{"issue": key, "normalized_ascii_sha256": sha256(text.encode("ascii")).hexdigest()}
                           for key, text in sorted(issue_texts.items())],
        "complete_date_span_mjd": [61199, 61233],
        "preserved_offset_mjd": [61218, 61219, 61220, 61221],
        "labs": [{"id": key, "observed_days": sorted(values), "offsets_tenths_ns": [
            int(values[day] * 10) if day in values else None for day in range(61218, 61222)
        ]} for key, values in sorted(laboratories.items())],
    }


def replay_rapid_utc_extract(document):
    """Recompute the summary from the small extract without fetching source files."""
    if document.get("schema_version") != 1 or document.get("complete_date_span_mjd") != [61199, 61233]:
        raise ValueError("Unsupported Rapid UTC extract.")
    if document.get("preserved_offset_mjd") != [61218, 61219, 61220, 61221]:
        raise ValueError("Unexpected Rapid UTC offset days.")
    labs = {}
    for row in document["labs"]:
        key = row["id"]
        if key in labs:
            raise ValueError("Duplicate laboratory in Rapid UTC extract.")
        observed = row["observed_days"]
        if any(type(day) is not int or not 61199 <= day <= 61233 for day in observed) or len(set(observed)) != len(observed):
            raise ValueError("Invalid observed dates in Rapid UTC extract.")
        # Only key membership matters outside the preserved four-day comparison.
        values = dict.fromkeys(observed)
        offsets = row["offsets_tenths_ns"]
        if len(offsets) != 4:
            raise ValueError("Rapid UTC extract requires four offsets per laboratory.")
        for day, offset in zip(range(61218, 61222), offsets):
            if day in values:
                if type(offset) is not int:
                    raise ValueError("Missing observed offset in Rapid UTC extract.")
                values[day] = Fraction(offset, 10)
            elif offset is not None:
                raise ValueError("Offset outside observed dates in Rapid UTC extract.")
        labs[key] = values
    return _rapid_utc_summary(labs)


def _rapid_utc_summary(laboratories):
    target_days = set(range(61199, 61234))
    complete = [values for values in laboratories.values() if set(values) == target_days]
    if not complete:
        raise ValueError("No laboratory has observations for the full date interval.")
    if "CH" not in laboratories or not all(day in laboratories["CH"] for day in range(61218, 61222)):
        raise ValueError("Bern-Wabern observations are missing.")
    changes = [median([lab[day + 1] - lab[day] for lab in complete]) for day in range(61218, 61221)]
    return {"complete_laboratories": len(complete),
            "any_data_laboratories": sum(bool(values) for values in laboratories.values()),
            "changes_27_to_30_jun_ns": [float(value) for value in changes],
            "bern_wabern_27_to_30_jun_ns": [float(laboratories["CH"][day]) for day in range(61218, 61222)]}


def _cenomanian(operation):
    question = "Find and correlate a dated local layer that brackets the transition. Check for reworked material and dating uncertainty."
    limits = ["The regional evidence has coarser spatial and temporal resolution than a single point at an instant.",
              "The 100.5–99.5 Ma bracket is a synthetic assumption.",
              "Quoted numerical boundary ranges belong to chart v2026/06 and are used as model bounds here."]
    bounds = {"older_boundary_ma": ["100.4", "100.6"], "younger_boundary_ma": ["93.7", "94.1"],
              "target_ma": "100.0", "units": "Millions of years before present", "chart_version": "2026/06"}
    source_ids = [ICS, REGIONAL, ASH, BRIEF]
    if operation == "PINPOINT":
        return _reply("cenomanian", operation, "AMBIGUOUS",
                      "The regional reconstruction supports a coastal plain. The chosen point could be dry or flooded at the target age.",
                      {"stage": "Cenomanian", "bounds": bounds, "regional": "Coastal plain near the western shore of the Western Interior Seaway, near the present-day Colorado Front Range.",
                       "possible_local_states": ["DRY", "FLOODED"], "synthetic_local_transition_ma": ["99.5", "100.5"]},
                      source_ids, limits, question)
    if operation == "TRAVEL":
        return _reply("cenomanian", operation, "CONDITIONAL",
                      "A local dated horizon plus a justified stratigraphic correlation can narrow the flooding interval.",
                      {"options": ["Inspect local sedimentary facies and identify a flooding surface",
                                   "Date a suitable primary ash layer and assess whether it was reworked",
                                   "Record interval overlap and retain both states if the bracket still contains 100.0 Ma"]},
                      source_ids, limits, question)
    if operation == "REVERSE":
        return _reply("cenomanian", operation, "HISTORICAL_VIEW",
                      "The regional evidence and the synthetic local bracket can be recovered as separate records.",
                      {"available_at_regional_only_checkpoint": {"regional_conclusion": "Regional coastal plain", "local_state": "UNKNOWN"},
                       "later_fixture": {"type": "ASSUMPTION", "synthetic_local_transition_ma": ["99.5", "100.5"],
                                         "possible_local_states": ["DRY", "FLOODED"]}},
                      source_ids, limits, question)
    membership = age_membership(bounds["target_ma"], bounds["younger_boundary_ma"], bounds["older_boundary_ma"])
    return _reply("cenomanian", operation, "PROVED_IN_BOUNDS",
                  "100.0 Ma lies inside the Cenomanian for every combination of the declared boundary ranges.",
                  {"property": "Interval membership", "bounds": bounds, "membership": membership,
                   "proof": "94.1 < 100.0 < 100.4 in Ma before present.",
                   "invalidation": "Recheck membership if chart version, numerical bounds or target age change.",
                   "local_flooding": "AMBIGUOUS"},
                  source_ids, limits, question)


def _archive(operation):
    limits = ["Synthetic uniform sample without replacement within each trial. The study assumes separate trials.",
              "Summary survival and regional claim recovery are separate from underlying evidence and provenance.",
              "The original random seed was unavailable. The independent re-run uses predeclared, different seeds."]
    next_check = "Test survival across independent failure domains and try rebuilding after losing the summary, frame, calibration and trust anchor."
    if operation == "PINPOINT":
        return _reply("archive", operation, "BOUNDED",
                      "Keeping 10 of 10,000 records leaves about a 0.599% chance of preserving any of the six decisive records.",
                      {"retained": 10, "population": 10000, "decisive": 6,
                       "exact_probability": str(retention_probability(10_000, 10, 6)),
                       "supplied_recoveries": 62, "supplied_trials": 10000,
                       "supplied_reproduction": "ORIGINAL_SEED_UNAVAILABLE"}, [BRIEF], limits, next_check)
    if operation == "TRAVEL":
        return _reply("archive", operation, "CONDITIONAL",
                      "A compact summary preserves meaning if it survives. Independent copies protect more of the source evidence.",
                      {"alternatives": [
                          {"design": "Two full copies", "storage_relative": "2", "failure_boundary": "Either copy independently survives."},
                          {"design": "Compact summary plus separate original", "storage_relative": "1 plus summary size", "failure_boundary": "A surviving summary alone gives bounded meaning and no original record bytes."},
                          {"design": "Two-of-three XOR coding", "storage_relative": "About 1.5 binary payloads plus framing and Base64",
                           "failure_boundary": "Recover one shard erasure with any two intact shards, a compatible decoder and a separately trusted digest.",
                           "status": "VERIFIED_BOUNDED_IMPLEMENTATION"},
                      ], "shared_failure_domains": ["Storage account", "Archive decoder", "Unit and frame registry", "Verification key"]},
                      [BRIEF], limits, next_check)
    if operation == "REVERSE":
        return _reply("archive", operation, "SIMULATED_RECOVERY",
                      "The survivor list determines what can still be understood, checked, or recovered.",
                      {"original_missing": archive_recovery(raw_record=False, summary=True, frame=True, calibration=True, trusted_key=True),
                       "targeted_removal": archive_recovery(raw_record=False, summary=False, frame=True, calibration=True, trusted_key=True),
                       "frame_missing": archive_recovery(raw_record=True, summary=True, frame=False, calibration=True, trusted_key=True),
                       "shared_calibration_loss": archive_recovery(raw_record=True, summary=True, frame=True, calibration=False, trusted_key=True),
                       "trust_anchor_missing_or_revoked": archive_recovery(raw_record=True, summary=True, frame=True, calibration=True, trusted_key=False)},
                      [BRIEF], limits, next_check)
    primary = archive_experiment()
    comparison = archive_experiment(seed=20261114)
    sample = b"100.0 Ma | Colorado Front Range | regional coastal plain | local state unresolved"
    shards = split_archive(sample)
    expected = sha256(sample).hexdigest()
    recovered = sum(restore_archive(shards[:lost] + shards[lost + 1:], expected_sha256=expected) == sample
                    for lost in range(3))
    return _reply("archive", operation, "VERIFIED_SYNTHETIC_MODEL",
                  "Exact combinatorics and two independently seeded simulations describe the specified random-loss model.",
                  {"probability_formula": "1 − C(N−D,k)/C(N,k), for N records, D decisive records, and k retained",
                   "independent_runs": [primary, comparison],
                   "supplied_only": {"trials": 10000, "random_hits": 62, "summary_hits_conditional_on_survival": 10000,
                                     "targeted_hits": 0, "original_seed": "UNKNOWN"},
                   "conditional_summary_recoveries": {"condition": "Summary, its frame and units survive", "trials": 10000, "meaning_recoveries": 10000,
                                                       "underlying_evidence_restored_by_summary": False},
                   "targeted_removal": {"removed": ["all six decisive records", "summary"], "recoveries": 0},
                   "shard_recovery": {"algorithm": ARCHIVE_SHARD_VERSION, "single_shard_losses": 3,
                                      "exact_recoveries": recovered, "original_size": len(sample),
                                      "original_sha256": expected,
                                      "shared_failure": "With two shards missing or no trusted digest, original bytes are unavailable."}},
                  [BRIEF], limits, next_check)


def _supplied_studies(operation):
    return _reply("supplied-studies", operation, "UNRESOLVED",
                  "The brief reports large timing and optimization runs. Their original inputs and logs are unavailable.",
                  {"supplied": {"timing_steps": 10000000, "reported_convergence_percent": "99.4",
                                "reported_latency_median_multiplier": "3.2", "modeled_years": 100000000,
                                "claimed_adversarial_trials": 10000000000},
                   "needed": ["Exact workload and model revision", "Random seeds and trial independence",
                              "Drift and clock skew models", "Decision/outcome denominators", "Raw traces and stopping rules"],
                   "scope": "Supplied hypotheses pending reproducible inputs and outputs."},
                  [BRIEF], ["Modeled years, wall-clock runtime, steps, trials and empirical observations are distinct."],
                  "Obtain a pinned workload with outcome definitions, then compare cutoffs on held-out scenarios.")


def dispatch(case, operation):
    """Expose the four pure, non-effectful operations for public fixtures."""
    if type(case) is not str or case not in CASES:
        raise ValueError("Unknown evidence fixture.")
    if type(operation) is not str or operation not in OPERATIONS:
        raise ValueError("Unknown Oracle operation.")
    handler = {"cern": _cern, "cenomanian": _cenomanian, "archive": _archive,
               "supplied-studies": _supplied_studies}[case]
    return handler(operation)


def fixture_document():
    return {"schema_version": 1,
            "cases": {case: {operation: dispatch(case, operation) for operation in OPERATIONS} for case in CASES}}
