"""Versioned CLI and loopback HTTP entry points for the public practice session."""

import argparse
from base64 import urlsafe_b64encode
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from os import urandom
import sys

from .adapters import austin, capabilities
from .bib_binding import export_bib_fixture, inspect_bib
from .codec import digest, loads, read_json
from .controller import Controller, ControllerError, compare_wait, opportunity, search_slice, verify_certificate, verify_checkpoint
from .decisions import demo
from .demo_records import CUTOFF, EVENT, timeline
from .evidence import dispatch
from .horizon import slice_horizon
from .live_failover import demo as live_failover_demo
from .records import VIEWS, view
from .releases import LIMIT, ReleaseStore, manifest
from .telemetry import demo as telemetry_demo

ACTOR = "practice"
OPERATIONS = ("PINPOINT", "TRAVEL", "REVERSE", "CERTIFY")


def operate(controller, request, *, actor=ACTOR):
    if not isinstance(request, dict):
        raise ValueError("Expected a request object")
    allowed = {
        "PINPOINT": set(), "TRAVEL": {"goal", "path", "program"}, "REVERSE": {"revision"},
        "CERTIFY": {"goal"}, "COMMIT": {"preview_id"}, "CONTEXT": {"now", "check_available"},
        "CHECKPOINT": set(), "WINDOW": set(),
    }
    operation = request.get("operation")
    if (not isinstance(operation, str) or operation not in allowed
            or set(request) - {"operation"} - allowed[operation]):
        raise ValueError("Unsupported operation or request field")
    fields = {key: value for key, value in request.items() if key != "operation"}
    if operation == "PINPOINT":
        return controller.pinpoint()
    if operation == "TRAVEL":
        return controller.travel(actor, **fields)
    if operation == "REVERSE":
        return controller.reverse(actor, **fields)
    if operation == "CERTIFY":
        return controller.certify(**fields)
    if operation == "COMMIT":
        if set(fields) != {"preview_id"}:
            raise ValueError("Commit requires a preview identifier")
        return controller.commit(actor, fields["preview_id"])
    if operation == "CONTEXT":
        return controller.set_context(**fields)
    if operation == "WINDOW":
        state = controller.pinpoint()
        return opportunity(state["now"], state["evidence"]["independent_check"], start=state["state"])
    return controller.checkpoint()


def local_server(controller, port=8767, *, token=None):
    """An authenticated local transport for bundled practice data only."""
    token = token or urlsafe_b64encode(urandom(32)).rstrip(b"=").decode("ascii")
    if not token.isascii() or len(token) < 32:
        raise ValueError("A sufficiently long local session token is required")

    class Handler(BaseHTTPRequestHandler):
        server_version = "FutureReader"
        sys_version = ""

        def log_message(self, *args):
            pass

        def response(self, code, data):
            body = json.dumps(data, ensure_ascii=True, allow_nan=False).encode("ascii")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.connection.settimeout(4)
            host = f"127.0.0.1:{self.server.server_port}"
            if (self.headers.get("Host") != host or self.headers.get("Origin") is not None
                    or not compare_digest(self.headers.get("Authorization", "").encode("utf-8"), b"Bearer " + token.encode("ascii"))):
                self.response(403, {"status": "FORBIDDEN"})
                return
            if self.path != "/v1/operation":
                self.response(404, {"status": "UNAVAILABLE"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if (self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json"
                        or not 1 <= size <= 32768):
                    raise ValueError("Expected bounded JSON")
                request = loads(self.rfile.read(size), limit=32768)
                self.response(200, operate(controller, request))
            except ControllerError as error:
                self.response(403 if error.reason == "FORBIDDEN" else 409,
                              {"status": error.reason, "detail": str(error)})
            except (ValueError, TypeError, TimeoutError) as error:
                self.response(400, {"status": "INVALID_INPUT", "detail": str(error)})

    return HTTPServer(("127.0.0.1", port), Handler), token


def _parser():
    parser = argparse.ArgumentParser(prog="python -m future_reader", description=__doc__)
    parser.add_argument("--store", type=Path, default=Path(".build/practice.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities")
    sub.add_parser("decisions")
    sub.add_parser("demo")
    sub.add_parser("telemetry-demo")
    sub.add_parser("live-failover-demo")
    for name in ("bib-inspect", "bib-export-fixture"):
        bib = sub.add_parser(name)
        bib.add_argument("path", type=Path)
        bib.add_argument("--bib-source", required=True, type=Path)
    sub.add_parser("release-demo")
    release = sub.add_parser("release")
    release.add_argument("--db", type=Path, default=Path(".build/releases.sqlite3"))
    release_actions = release.add_subparsers(dest="release_action", required=True)
    for name in ("preview", "apply"):
        action = release_actions.add_parser(name)
        action.add_argument("manifest", type=Path)
        action.add_argument("html", type=Path)
        action.add_argument("--trusted-pin", required=True)
        if name == "apply":
            action.add_argument("--preview", type=Path, required=True)
            action.add_argument("--approval", required=True)
    release_actions.add_parser("status")
    rollback = release_actions.add_parser("rollback")
    rollback.add_argument("--generation", type=int, required=True)
    rollback.add_argument("--checkpoint-schema", required=True)
    extract = release_actions.add_parser("extract")
    extract.add_argument("output", type=Path)
    server = sub.add_parser("serve")
    server.add_argument("--port", type=int, default=8767)
    operation = sub.add_parser("operate")
    operation.add_argument("json", help="A JSON object or @ followed by a JSON file path")
    case = sub.add_parser("case")
    case.add_argument("case", choices=("cern", "cenomanian", "archive", "supplied-studies"))
    case.add_argument("operation", choices=OPERATIONS)
    temporal = sub.add_parser("view")
    temporal.add_argument("view", choices=tuple(VIEWS))
    temporal.add_argument("--as-of", default=CUTOFF)
    temporal.add_argument("--event-at", default=EVENT)
    adapter = sub.add_parser("austin")
    adapter.add_argument("--now", type=int, default=0)
    adapter.add_argument("--without-check", action="store_true")
    adapter.add_argument("--compile", action="store_true")
    verify = sub.add_parser("verify-certificate")
    verify.add_argument("path", type=Path)
    frontier = sub.add_parser("frontier")
    frontier.add_argument("--budget", type=int, default=1)
    frontier.add_argument("--now", type=int, default=0)
    frontier.add_argument("--without-check", action="store_true")
    frontier.add_argument("--resume", type=Path)
    horizon = sub.add_parser("horizon")
    horizon.add_argument("--opening", type=int, default=0)
    horizon.add_argument("--budget", type=int, default=4)
    horizon.add_argument("--resume", type=Path)
    wait = sub.add_parser("compare-wait")
    wait.add_argument("--now", type=int, default=0)
    wait.add_argument("--check-returns", type=int, required=True)
    handoff = sub.add_parser("handoff")
    handoff.add_argument("target_session")
    handoff.add_argument("checkpoint_sha256")
    checkpoint = sub.add_parser("verify-checkpoint")
    checkpoint.add_argument("path", type=Path)
    checkpoint.add_argument("--sha256")
    return parser


def _demo():
    with Controller() as controller:
        controller.set_grant(ACTOR, True)
        before = controller.pinpoint()
        plan = controller.travel(ACTOR)
        moved = controller.commit(ACTOR, plan["preview_id"])
        undo = controller.reverse(ACTOR)
        restored = controller.commit(ACTOR, undo["preview_id"])
        certificate = controller.certify()
        return {"initial": before, "preview": plan, "travel": moved, "restore_preview": undo,
                "restored": restored, "certificate": certificate, "verified": verify_certificate(certificate)}


def release_demo():
    """Exercise approval, atomic switching and rollback on disposable fixture bytes."""
    old = b"<!doctype html><title>Illustrative Future Reader 0.1.0</title>"
    new = b"<!doctype html><title>Illustrative Future Reader 0.1.1</title>"
    with ReleaseStore(":memory:") as store:
        for raw, version in ((old, "0.1.0"), (new, "0.1.1")):
            entry = manifest(raw, version)
            pin = digest(entry)
            preview = store.preview(entry, raw, trusted_manifest_sha256=pin)
            switched = store.apply(entry, raw, preview, trusted_manifest_sha256=pin,
                                   approval_digest=preview["approval_digest"])
        restored = store.rollback(expected_generation=switched["generation"],
                                  compatible_checkpoint_schema=entry["checkpoint_schema"])
        return {"preview": preview, "switched": switched, "rollback": restored,
                "verified": store.active_bytes() == old, "mode": "LOCAL_RELEASE_FIXTURE"}


def _release(args):
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with ReleaseStore(args.db) as store:
        if args.release_action == "status":
            return store.status()
        if args.release_action == "rollback":
            return store.rollback(expected_generation=args.generation,
                                  compatible_checkpoint_schema=args.checkpoint_schema)
        if args.release_action == "extract":
            raw = store.active_bytes()
            with args.output.open("xb") as stream:
                stream.write(raw)
            return {"status": "EXTRACTED", "bytes": len(raw)}
        value = read_json(args.manifest)
        with args.html.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
        if args.release_action == "preview":
            return store.preview(value, raw, trusted_manifest_sha256=args.trusted_pin)
        return store.apply(value, raw, read_json(args.preview), trusted_manifest_sha256=args.trusted_pin,
                           approval_digest=args.approval)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "capabilities":
            result = capabilities()
        elif args.command == "decisions":
            result = demo()
        elif args.command == "case":
            result = dispatch(args.case, args.operation)
        elif args.command == "view":
            result = view(timeline(), args.view, actor="reader", as_of=args.as_of, event_at=args.event_at)
        elif args.command == "austin":
            result = austin(now=args.now, check_available=not args.without_check, compile_qiskit=args.compile)
        elif args.command == "verify-certificate":
            result = {"verified": verify_certificate(read_json(args.path))}
        elif args.command == "verify-checkpoint":
            result = {"verified": verify_checkpoint(read_json(args.path), expected_sha256=args.sha256)}
        elif args.command == "frontier":
            checkpoint = read_json(args.resume) if args.resume else None
            result = search_slice(now=args.now, check_available=not args.without_check, budget=args.budget, checkpoint=checkpoint)
        elif args.command == "horizon":
            checkpoint = read_json(args.resume) if args.resume else None
            result = slice_horizon(opening=args.opening, budget=args.budget, checkpoint=checkpoint)
        elif args.command == "demo":
            result = _demo()
        elif args.command == "telemetry-demo":
            result = telemetry_demo()
        elif args.command == "live-failover-demo":
            args.store.parent.mkdir(parents=True, exist_ok=True)
            result = live_failover_demo(args.store)
        elif args.command == "bib-inspect":
            result = inspect_bib(args.path, bib_source=args.bib_source)
        elif args.command == "bib-export-fixture":
            result = export_bib_fixture(args.path, bib_source=args.bib_source)
        elif args.command == "release-demo":
            result = release_demo()
        elif args.command == "release":
            result = _release(args)
        elif args.command == "compare-wait":
            result = compare_wait(args.now, check_returns=args.check_returns)
        else:
            args.store.parent.mkdir(parents=True, exist_ok=True)
            with Controller(args.store) as controller:
                controller.set_grant(ACTOR, True)
                if args.command == "serve":
                    server, token = local_server(controller, args.port)
                    with server:
                        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/v1/operation",
                                          "authorization": f"Bearer {token}", "mode": "SIMULATION"}), flush=True)
                        try:
                            server.serve_forever()
                        except KeyboardInterrupt:
                            pass
                    return 0
                if args.command == "handoff":
                    result = controller.handoff(ACTOR, args.target_session, expected_sha256=args.checkpoint_sha256)
                else:
                    request = read_json(args.json[1:]) if args.json.startswith("@") else loads(args.json)
                    result = operate(controller, request)
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return 0 if result.get("verified", True) else 1
    except (ValueError, PermissionError, TypeError, OSError) as error:
        print(json.dumps({"status": getattr(error, "reason", "ERROR"), "detail": str(error)}), file=sys.stderr)
        return 1
