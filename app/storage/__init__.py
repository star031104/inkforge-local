"""Persistence: the SQLite project repository (schema migrations, projects
and revisions, director tasks, context snapshots and chapter sessions,
full-text search, database backups), the project document schema and
backward-compatible migrations, project-scoped credentials, auxiliary
extension records, periodic backups, the backup location and the
single-writer instance lock.

May depend on :mod:`app.core`, :mod:`app.llm` and :mod:`app.story`; never on
planning, writing, review, director, io or api."""
