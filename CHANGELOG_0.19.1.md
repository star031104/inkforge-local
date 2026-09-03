# InkForge 0.19.1 — Release-Gate Reliability

0.19.1 closes the deterministic release blockers found by the first real
SiliconFlow end-to-end run. It does not weaken the evidence rules that protect
long-form memory.

## Fixed

- Use one adaptive synopsis-length contract in both staged auto-director volume
  generation steps, preventing a valid short-book core from failing only after
  its detail list has been generated.
- Prevent the local quality gate from comparing a saved candidate draft with
  itself and reporting every sentence as copied source prose.
- Add a narrow pre-unification authority gate for generated character and world
  assets. Obvious later titles and regalia are rejected before they can become
  high-priority canon.

## Changed

- Replace the oversized two-book incubator response with a staged transaction:
  two compact story cores are generated first, followed by one bounded
  character/world asset package for each direction. Nothing is persisted until
  all three stages validate.
- Replace the previous still-large memory fallback with a genuine minimum fact
  package: chapter summary, up to four evidence-backed facts, up to three
  character deltas, and a compact scene settlement. The rolling story digest is
  reconstructed locally when the recovery model omits it.
- Expose `generation_mode=staged_transaction` for incubator diagnostics.

## Verification

- 168 local automated tests pass.
- The staged incubator, adaptive volume boundary, minimal memory recovery,
  historical asset gate, and quality self-comparison guard all have regression
  coverage.
- A post-change cloud rerun reached the provider but received HTTP 401 from the
  currently available environment credential. A fresh authorized credential is
  therefore still required before 0.19.1 can be marked cloud-release approved.
