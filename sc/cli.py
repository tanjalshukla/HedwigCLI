from __future__ import annotations

import typer

from .commands.admin import (
    ask,
    constraints,
    constraints_relax,
    constraints_clear,
    doctor,
    guidelines,
    guidelines_clear,
    guidelines_suggest,
    import_rules,
    init,
    rules_list,
    set_mode,
    set_verification_cmd,
)
from .commands.observe import (
    checkin_stats,
    clear_traces,
    demo_seed,
    export,
    explain,
    leases,
    preferences,
    preferences_clear,
    report,
    reset,
    revoke,
    traces,
)
from .run.command import run

"""Command router with a compact public surface and legacy aliases."""

app = typer.Typer(add_completion=False)

config_app = typer.Typer(help="Configuration commands.")
rules_app = typer.Typer(help="Rule management.", invoke_without_command=True)
observe_app = typer.Typer(help="Observability commands.")


def _register_hidden_aliases(root: typer.Typer, aliases: dict[str, object]) -> None:
    for name, command in aliases.items():
        root.command(name, hidden=True)(command)


@rules_app.callback()
def _rules_default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        rules_list(json_out=False)


# Compact public surface.
app.command()(init)
app.command()(doctor)
app.command()(ask)
app.command()(run)
app.command()(report)
app.command()(reset)

config_app.command("set-mode")(set_mode)
config_app.command("set-verification-cmd")(set_verification_cmd)

rules_app.command("list")(rules_list)
rules_app.command("import")(import_rules)
rules_app.command("clear")(constraints_clear)
rules_app.command("suggest")(guidelines_suggest)
# Keep specific subcommands for advanced usage.
rules_app.command("constraints", hidden=True)(constraints)
rules_app.command("constraints-relax", hidden=True)(constraints_relax)
rules_app.command("constraints-clear", hidden=True)(constraints_clear)
rules_app.command("guidelines", hidden=True)(guidelines)
rules_app.command("guidelines-suggest", hidden=True)(guidelines_suggest)
rules_app.command("guidelines-clear", hidden=True)(guidelines_clear)

observe_app.command("leases")(leases)
observe_app.command("traces")(traces)
observe_app.command("explain")(explain)
observe_app.command("checkin-stats")(checkin_stats)
observe_app.command("preferences")(preferences)
observe_app.command("preferences-clear")(preferences_clear)
observe_app.command("clear-traces")(clear_traces)
observe_app.command("report")(report)
observe_app.command("revoke")(revoke)
observe_app.command("export")(export)
observe_app.command("reset-study-state")(reset)

app.add_typer(config_app, name="config")
app.add_typer(rules_app, name="rules")
app.add_typer(observe_app, name="observe")

_register_hidden_aliases(
    app,
    {
        "preferences": preferences,
        "start": run,
        "set-mode": set_mode,
        "set-verification-cmd": set_verification_cmd,
        "import-rules": import_rules,
        "import": import_rules,
        "constraints": constraints,
        "constraints-relax": constraints_relax,
        "constraints-clear": constraints_clear,
        "guidelines": guidelines,
        "guidelines-suggest": guidelines_suggest,
        "guidelines-clear": guidelines_clear,
        "leases": leases,
        "traces": traces,
        "explain": explain,
        "checkin-stats": checkin_stats,
        "preferences-clear": preferences_clear,
        "clear-traces": clear_traces,
        "demo-seed": demo_seed,
        "revoke": revoke,
    },
)
