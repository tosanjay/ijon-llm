"""IJON-Reloaded — LLM-guided IJON annotation for AFL++ (Mode 2 / standalone CLIs).

The deterministic harness lives in the sibling `harness` package; this package holds
the runnable CLIs (run_target, campaign_supervisor, campaign_cli, bringup, build_doctor,
analyst_cli, triage_crashes). The umbrella entry point is `ijon_reloaded.cli:main`
(exposed as the `ijon-reloaded` console script).
"""
