"""``tripl install`` and ``tripl upgrade``: the two commands that touch a HOST.

Every other command in this CLI acts on a running instance over HTTP. These two
act on a DIRECTORY and on the local Docker daemon, which is a different category
with different rules, so they live in their own package rather than beside
``doctor``.

Three decisions worth stating once, here, rather than re-arguing at each call
site:

* IT SHELLS OUT TO ``docker compose``. No docker SDK, no PyYAML, no
  python-dotenv. ``httpx`` is this distribution's ONLY runtime dependency and
  that is a budget rather than an accident: tripl-mcp depends on this package,
  so a dependency added here is forced onto every MCP-server
  install too. ``subprocess`` and ``importlib.resources`` are stdlib and cost
  nobody anything.
* THE COMPOSE FILE IS A PACKAGED ASSET, COPIED BYTE FOR BYTE — not a template,
  not generated, not parsed. A generator would be a second description of the
  production stack, and the two would drift the first time somebody added a
  service to only one of them. ``tests/test_contract.py`` pins the packaged copy
  against the repository's own ``compose.yaml``, so editing one and not the
  other fails CI loudly.
* EVERY COMMAND IT RUNS IS ONE THE OPERATOR CAN PASTE. ``cwd`` is the install
  directory, there is no ``-f`` flag and no injected options beyond ``-d``, and
  the exact invocation is printed before it runs. That is what makes "the
  command above is safe to re-run by hand" a true statement rather than a
  comforting one, and it is why compose's own ``compose.override.yaml`` discovery
  keeps working.
"""
