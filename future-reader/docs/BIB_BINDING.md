# Beyond Infinity’s Bounds ledger binding

The optional binding consumes a caller-selected local native JSON array.
It has no automatic ledger discovery. Pass the reviewed BIB `src`
directory or its `bib/ledger.py` explicitly. The known native prototype
comes from the separate workspace `projects/beyond-infinities-bounds`.
Its 20-file patch manifest and all 13 existing tests passed locally.

`inspect_bib(ledger, bib_source=source)` checks the native source file
against the reviewed implementation digest and runs the pinned bytes in
an isolated child interpreter. It bounds the JSON input and checks the
native shape, sequence, links and entry digests. It returns the original
file digest and chain head alongside the native claim, time, evidence,
uncertainty, review and relationship fields. The native `AS`,
`BECOMING_IS`, `TO_BE` and `WAS` labels stay in their original field.
Future Reader assigns no new ChronoTense standing, review step, causal
edge or availability time to a supplied ledger. The binding changes
neither the source implementation nor any existing ledger.

`export_bib_fixture(destination, bib_source=source)` creates a two-entry
synthetic native chain, validates the staged bytes, then publishes the
new file without replacing any destination. It requires a filesystem that
supports hard links for the final no-overwrite step. The child interpreter
isolates imports and avoids writing bytecode. It uses a pinned, inspected
implementation and does not provide an operating-system sandbox.

Verification checks consistency with the supplied file and its native
contract. It gives no publisher identity, external claim proof, automatic
access, or grant to execute. If the native code changes, inspect the
revision and run conformance before adding a supported digest.

Run the local test from the Future Reader folder.

```powershell
$env:FUTURE_READER_BIB_SOURCE = (Resolve-Path -LiteralPath '../../../../../projects/beyond-infinities-bounds/src').Path
python -B -m unittest discover -s tests -p test_bib_binding.py -v
```
