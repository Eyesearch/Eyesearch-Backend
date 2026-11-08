"""The crawl pipeline, in the order a URL travels through it.

admission (frontier, robots, duplicate check) -> fetch -> parse -> brand ->
quality gate -> classify -> language -> write. The files keep their numbers so
the order is readable in the listing; `load_agent()` resolves the old flat names
to this package, so no caller had to change.
"""
