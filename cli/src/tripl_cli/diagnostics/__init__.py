"""``tripl doctor``'s verdict machinery, plus the async read layer it shares.

The load-bearing rule of this package is a two-layer split:

* ``collect`` is **async and impure**. It is the only thing that speaks HTTP,
  and it turns every failure into data (``Fetched``) rather than an exception.
* ``checks`` / ``scan_checks`` are **pure, synchronous and total**. They take a
  ``Snapshot`` plus an explicit ``now`` and return findings. No IO, no clock, no
  ``await``.

That split is why the rule tests need neither a network nor a mock: the
interesting half of doctor is a function from a dict to a list of findings. It
is also why a check may read data another check "owns" without any ordering
hazard — everything was fetched before any check ran.

What is NOT here, and must not come back: the ``--json`` builders
(``tripl_cli.report``), the ASCII output (``tripl_cli.render``) and the shared
vocabulary (``tripl_cli.model``). Only ``doctor`` reaches a verdict, so filing
the ``scans``/``drifts``/``status``/``install`` documents, tables and snapshots
under a package named ``diagnostics`` made the name a false claim about most of
what it held. ``tests/test_contract.py`` pins this package's module
list as a closed set, so the next verdict-free thing fails CI instead of
accreting here.

``collect`` and ``endpoints`` stayed despite ``status``, ``scans``, ``drifts``,
``watch`` and ``install`` all reading them: the ``Fetched`` regime is one half of
doctor's totality guarantee and ``checks`` is the other, so moving it would put
one rule in two packages. What those commands borrow is that discipline, which
is the reason it is worth borrowing.
"""
