"""``tripl codegen``: typed tracking code generated from the plan.

``model`` reads the ``codegen_model`` export, ``naming`` turns plan strings
into identifiers, ``context``/``context_named`` build what a template sees per
event-type style, ``template`` is the Mustache subset that renders it, and
``generate``/``files`` produce and write (or ``--check``) the files. Built-in
templates live in ``templates/`` as package data.
"""
