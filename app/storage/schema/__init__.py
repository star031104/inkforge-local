"""The persisted shape of a project document.

``project.ensure_project_defaults`` composes the defaults and normalisers of
every section.  Sections owned by feature packages (planning, score
calibration, writing skills) keep their *data shape* here so that storage
never imports feature logic; the feature modules import these helpers back.
Normalisers only add or clean known keys -- unknown keys written by older or
newer versions are preserved."""
