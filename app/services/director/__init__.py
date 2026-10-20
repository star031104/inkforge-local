"""Automatic-director runtime, split by concern.

``app.services.director_runtime`` remains the public import surface; it
re-exports from here and owns the module attributes that tests monkeypatch.

Layout (each module's ``install(ctx)`` defines closures over a shared
:class:`~.context.DirectorContext`, in dependency order):

* ``common``     – constants, shared dataclasses/errors, pure helpers
* ``state``      – task persistence, events, planning/quality debts
* ``support``    – memory settlement/repair, experience bank, hooks, re-plan
* ``prose``      – per-scene compact prompts, rehearsal, anti-slop, best-of-n
* ``refinement`` – whole-book AI refinement
* ``incubator``  – incubation tasks and the incubator phase
* ``master``     – master-plan and volume-route phases
* ``chapters``   – per-chapter write → audit → revise → settle loop
* ``runner``     – task runner (lease, phase dispatch, pause/failure)
* ``control``    – HTTP-facing start/refine/poll/pause/resume
* ``runtime``    – wiring of the above into a ``DirectorRuntime``
"""
