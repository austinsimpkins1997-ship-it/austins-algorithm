# Local release and rollback

The source repository, release downloads, and package registry have separate
access rules. The existing Austin repository was reachable publicly when
reviewed on November 13, 2026. Its public Releases page showed no releases.
Future Reader has local source, a portable HTML file, and local build
artifacts. No hosted Future Reader asset, registry coordinate, signed
manifest, or authorized private-access test was available. GitHub Releases
can deliver the HTML as an asset. The GitHub Packages permission guide
documents separate package access and supported registries.
[Austin source](https://github.com/austinsimpkins1997-ship-it/austins-algorithm) ·
[Public releases](https://github.com/austinsimpkins1997-ship-it/austins-algorithm/releases) ·
[Releases API](https://docs.github.com/en/rest/releases/releases) ·
[Package permissions](https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages)

The local manifest records source commit when available, artifact byte
count and SHA-256, schema compatibility, migration, and rollback. A
caller-supplied manifest pin binds approval to an exact manifest. Obtain
the pin independently if using it as publisher trust. The supplied local
receipt records an unsigned build from a working tree.

### Preview, verify, and roll back

1. Export a browser checkpoint. Save it with the corresponding application
   version and manifest.
2. Generate or obtain a candidate manifest and independently trusted pin.
   Run `python -m future_reader release preview MANIFEST HTML --trusted-pin PIN`.
   Save the JSON preview. Review its compatibility and its approval digest.
3. Apply the reviewed artifact with `python -m future_reader release apply
   MANIFEST HTML --trusted-pin PIN --preview PREVIEW --approval DIGEST`.
   An atomic SQLite transaction verifies the candidate and advances the
   active pointer while retaining the preceding verified bytes.
4. Extract the verified active HTML to a new path with
   `python -m future_reader release extract NEW_FILE`.
5. To roll back, inspect `release status`, then run `release rollback
   --generation NUMBER --checkpoint-schema future-reader.browser-checkpoint/1`.
   Extract the now-active bytes and reopen a compatible checkpoint.

`release-demo` switches two disposable fixtures and verifies the rollback.
The browser displays a release preview example. Local application of
reviewed artifacts goes through the CLI.

For the public Austin repository, publish the matching commit and its
`workbench-v0.2.3-alpha` tag through GitHub Desktop. The tag launches the
repository release workflow. It verifies the executable and offline
companion, stages the assets with fingerprints and the source commit, and
publishes a GitHub prerelease. Repository availability and a successful
release run are separate checks. [GitHub Desktop tags](https://docs.github.com/en/desktop/managing-commits/managing-tags-in-github-desktop)

The Rapid UTC compact extract is a selection and transformation of
BIPM Time Department records for issues 2624–2628. Credit goes to the
BIPM and contributing laboratories. The original tables are linked in the
evidence guide. [BIPM reuse terms](https://www.bipm.org/en/copyright)

### Compatibility

| Surface | Scope checked | Evidence boundary |
| --- | --- | --- |
| Python core and CLI | Windows CPython 3.10, 3.12, 3.14 | Bounded bundled cases and local records |
| Windows Workbench | Bundled Python/Tk on Windows x64 | Unsigned local alpha, no installer or managed rollout |
| Browser companion | Chrome, local file, offline, keyboard and 390 px viewport | Screen-reader user and child usability sessions pending |
| Austin weighted project and exports | Existing 0.2.0-alpha wire format | Native practice has a separate SQLite and checkpoint schema |
| Qiskit/OpenQASM | Local target basis, small reference circuit | Simulator verified |
| Beyond Infinity’s Bounds | Reviewed v0.0.1 native ledger | Caller-selected local input and synthetic round trip |
| Hardware and distributed workers | No configured verified backend | Planned or unavailable |

Old Austin weighted projects retain their prior replay contract. Future
Reader schema 1 rejects incompatible checkpoints. It has no earlier
released native practice schema to migrate. External licenses and
distribution rights require review before publication. Original briefs
are included for this authorized local handoff.
