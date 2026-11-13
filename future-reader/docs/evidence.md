# Evidence fixtures and reconstruction

The fixture data supports the four controller operations through
`future_reader.evidence.dispatch(case, operation)`. `fixtures/evidence.json`
contains the same results for use offline. All four cases are public practice
records. The fixture does not operate any instrument.

## CERN timing

The October 4, 2024 schedule announced a June 2030 Run 4 target. A June 29,
2026 operations report identifies June 27 as the date of the last beam
dump. A separate June 29 announcement marks the LHC's entry into Long
Shutdown 3. The reverse view at the announcement cutoff includes the
published plan and excludes the two later reports. [CERN schedule](https://home.cern/updated-schedule-cerns-accelerators/) · [last beams report](https://home.cern/so-long-and-thanks-for-all-the-collisions-2/) · [shutdown announcement](https://home.cern/cern-bids-farewell-to-the-lhc-and-enters-long-shutdown-3/)

For distance `L` and hypothetical ratio `r = v/c`, a slower flight over
`L` and a flight over `L/r` at `c` both take `L/(r*c)`. At `L = 11.42 m`
and `r = 0.996`, the exact extra distance is `571/12450 m` and the exact
extra time is `571/3732416102100 s`. The flight-time certificate covers
this equality with a common endpoint time reference. It preserves two
compatible explanations for an arrival-time-only reading. A geometric
survey must independently bound the path uncertainty well below the
modeled difference of about 4.6 cm before it can distinguish them.
[BIPM definition of the metre](https://www.bipm.org/en/si-base-units/metre)

The hypothetical detector example uses the controller's critical-path
function. With a 120-second deadline, parallel survey and calibration,
and a 10-second margin, its latest safe start is 5 seconds after the
opening. A prerequisite arriving at 6 seconds triggers the fallback.
A detector queue of 15 seconds puts the start on the equality boundary.
An expiring calibration can close the same window earlier than the
deadline. This is an operational simulation with declared inputs.

The literal 11.42 m input gives 38.0930 ns at `c` and 38.2460 ns at
`0.996c`. The supplied summary lists 38.091 and 38.244 ns. Its unrounded
path is unavailable. The extra time is about 153 ps at the stated
precision. The fixture carries the supplied 10,000- and 100,000-trajectory
outputs, timing comparison and slope trials as reported synthetic
results. Their original samplers, records and implementations were
unavailable for replay.

The machine parameters describe a nominal dipole and beam screen. The
26.7 km ring circumference and the modeled first-wall-hit path refer to
different geometries. The sector pressure during shutdown remains unknown.
[LHC design report](https://cds.cern.ch/record/1129806/files/jinst8_08_s08001.pdf) · [HiLumi design report](https://e-publishing.cern.ch/index.php/CYRM/article/download/1188/986/4985) · [CERN vacuum systems](https://home.cern/science/engineering/vacuum-empty-interstellar-space)

The 1964 gamma-ray paper reported 299,770 ± 40 km/s over 31.450 ± 0.0015 m
and described air, beryllium and Mylar in the setup. The corresponding
model delay at `0.996c` is 0.421 ns. The 1968 National Accelerator
Laboratory design report describes 10 BeV injection protons moving at
about 99.6% of `c` on PDF page 81. The source for the supplied CMS
sample-purity figure is unresolved. No modern LHC photon speed measurement
at `0.996c` was established by these records.
[1964 paper](https://www.sciencedirect.com/science/article/abs/pii/0031916364910959) · [1968 design report](https://lss.fnal.gov/archive/design/fermilab-design-1968-01.pdf)

The 1977 paper concerns relativistic muon lifetimes. CERN attributed the
original OPERA discrepancy to fibre-optic timing equipment. OPERA's
dedicated follow-up was compatible with zero timing discrepancy within
the reported uncertainty. The structured light study measured group
delay along the optical axis. Each result retains its experimental
observable. [Muon lifetime study](https://www.nature.com/articles/268301a0) · [CERN OPERA update](https://home.cern/opera-experiment-reports-anomaly-in-flight-time-of-neutrinos-from-cern-to-gran-sasso/) · [OPERA follow-up](https://arxiv.org/abs/1212.1276v2) · [Structured light study](https://arxiv.org/abs/1411.3987)

The Rapid UTC calculation used issues 2624 through 2628. Of 67
laboratories with observations, 59 had values on every day from June 8
to July 12, 2026. Using those 59 labs, the median differences of daily
offsets `UTCr−UTC(k)` for June 27→28, June 28→29 and June 29→30 are
−0.1, +0.1 and −0.3 ns. Bern-Wabern's offsets over those four days
were 0.8, 0.8, 0.9 and 0.9 ns. Circular T's `UTC−UTC(k)` for Bern
was 0.8 ns on each of MJD 61214, 61219 and 61224. The BIPM published
adjacent monthly fractional TAI scale-interval estimates of
(−0.30 ± 0.09) × 10⁻¹⁵ and (−0.09 ± 0.12) × 10⁻¹⁵. Daily relative
offsets and monthly estimates cannot resolve a short-lived, local or
shared reference effect. The small extract preserves the four daily
offsets, complete-case coverage and source hashes. It supports offline
replay of these medians and does not contain the complete source tables.
[Rapid UTC issues](https://webtai.bipm.org/ftp/pub/tai/Rapid-UTC/utcr/) · [Circular T 462](https://webtai.bipm.org/ftp/pub/tai/Circular-T/cirt/cirt.462) · [Circular T 463](https://webtai.bipm.org/ftp/pub/tai/Circular-T/cirt/cirt.463)

## Early Cenomanian

The chart v2026/06 gives 100.5 ± 0.1 Ma and 93.9 ± 0.2 Ma for the older
and younger Cenomanian boundaries. Treating those reported uncertainties
as the ranges of this fixture, 100.0 Ma lies strictly inside for every
combination. The regional reconstruction supports a coastal plain along
the Western Interior Seaway near today's Colorado Front Range. The dry
and flooded alternatives remain compatible for a point whose synthetic
transition bracket is 99.5–100.5 Ma. A dated layer can narrow the bracket
where local stratigraphy and correlation justify it.
[ICS v2026/06](https://stratigraphy.org/chart/?scale=linear) · [NPS regional reconstruction](https://www.nps.gov/subjects/fossils/dinosaur-freeway.htm) · [NPS fossil dating context](https://www.nps.gov/subjects/fossils/volcanoes-and-fossils.htm)

## Archive loss

The exact chance of retaining at least one of six decisive records in a
uniform draw of ten records from ten thousand is 0.5986513%. Two new
seeded runs gave 61 and 68 recoveries in 10,000 trials each. Both 95%
Wilson intervals include the analytical probability. The supplied 62
recoveries remain labeled separately because the original seed was
unavailable. The sampler is specified in the fixture so another runtime
can reproduce it from the published seeds.

A surviving summary and its frame recover bounded regional meaning in
this model. Targeted removal of both the decisive originals and summary
leaves that claim unrecoverable. Removing the frame can leave meaning
ambiguous even if bytes survive. A missing or revoked trust anchor can
leave provenance unresolved while raw evidence remains. The comparison
includes duplication, separate summaries and bounded two-of-three XOR
coding. The XOR implementation keeps two equally sized data shards and
one parity shard, with zero padding for odd byte counts. The proof
replayed all three single-shard losses and recovered the exact original
bytes. It requires a compatible decoder, any two intact shards and a
separately trusted digest. Two missing shards leave reconstruction
unavailable. Its byte proof does not authenticate the digest or resolve
the geological claim. The nominal storage cost is about 1.5 times the
binary payload, plus metadata, padding and Base64 overhead.

## Re-run locally

```powershell
python -B -m unittest discover -s tests -p test_evidence.py -v
```

The test reads the compact source extract from disk, recomputes the four
daily offsets and the three complete-case medians, and checks the
offline fixture against the controller's current output. The provided
checks cover exact calculations, bounded archive simulations and the
published source comparisons named above. The supplied 10 million timing
steps, 100 million modeled years, 10 billion adversarial trials and
99.4% convergence claim remain unresolved until their pinned inputs,
workload and output traces are available.
