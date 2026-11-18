import typer

feature_app = typer.Typer(invoke_without_command=True, help="Feature workflow (not implemented yet).")


@feature_app.callback()
def feature(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
