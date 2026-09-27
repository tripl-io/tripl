"""``tripl check``: static and payload validation against the tracking plan (#261).

Layers, each importable without the ones after it:

``yamlish``   the YAML subset ``.tripl/check.yml`` is read with (no dependency).
``config``    the config model: per event type, how its calls look.
``presets``   Segment, Amplitude and Snowplow call specs, in the config's own vocabulary.
``lexer``     one tokenizer per language family (strings, interpolation, comments).
``calls``     call sites and Objective-C message sends out of a token stream.
``symbols``   enum raw values, string constants, string-returning functions.
``values``    an argument's tokens -> a known string, a choice, a dict, or dynamic.
``scan``      files -> call sites -> validation items (static mode).
``payloads``  captured events -> validation items (payload mode).
``validate``  batching to ``POST /plan/validate`` and mapping verdicts back.
``render``    the human listing and SARIF 2.1.0; ``--json`` lives in ``tripl_cli.report``.

Nothing before ``validate`` touches the network.
"""
