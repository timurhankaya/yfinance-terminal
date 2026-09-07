"""`yfin api client` command group.

Thin wrappers over `api/storage/clients.py`, for the same reason
`yfin config` is a thin wrapper over `settings_store`: the self-service
portal will call that module directly, and any rule buried in a command
would be a rule the portal silently bypasses.

Two behaviours are worth stating outright.

A secret is printed exactly once, to stdout, and never stored in the
clear. There is no command to recover it -- if it is lost, rotate.

`disable` and `revoke` are not done when the database row is written.
Token verification does not read the database, so the change only takes
effect once Redis carries it. If that write fails, the command exits
non-zero and says so: an operator who believes they have cut off a
misbehaving client, but has not, is worse off than one who knows the
command failed.
"""

from __future__ import annotations

from typing import Annotated

import typer
from sqlalchemy.orm import Session, sessionmaker

from yfin.cli.common import session_factory
from yfin.core.families import DataFamily, scope_for
from yfin.core.logging_setup import get_logger

log = get_logger(__name__)

api_app = typer.Typer(help="Read API administration", no_args_is_help=True)
client_app = typer.Typer(help="API client credentials", no_args_is_help=True)
usage_app = typer.Typer(help="Measured API usage", no_args_is_help=True)
api_app.add_typer(client_app, name="client")
api_app.add_typer(usage_app, name="usage")

EXIT_REJECTED = 2
#: The DB row is written but the change has not propagated to Redis.
EXIT_NOT_PROPAGATED = 3


def _session_factory() -> sessionmaker[Session]:
    return session_factory()


def _scope_values() -> list[str]:
    """The scope names, derived the same way `ApiScope` derives them.

    Not read off `ApiScope` itself, which lives in the ORM module: this
    function is called while the command decorators are being evaluated,
    so importing it here made `yfin --help` load the whole model package.
    `api/models/clients.py` builds the enum from exactly this pair, and
    `test_api_clients.py` asserts the two lists stay equal.
    """
    return [scope_for(family) for family in DataFamily]


def _propagate(
    client_id: str,
    epoch: int,
    *,
    disabled: bool | None = None,
    revoked_secret_ids: tuple[int, ...] = (),
) -> bool:
    """Publishes an authorisation change to Redis so live tokens stop.

    Returns False instead of raising: the database change is already
    committed, so the honest report is a partial success, not a failure
    of the whole operation.
    """
    from yfin.api.core.config import get_api_settings
    from yfin.api.ratelimit.revocation import WrongRedis, publish_revocation

    try:
        publish_revocation(
            get_api_settings(),
            client_id,
            epoch=epoch,
            disabled=disabled,
            revoked_secret_ids=revoked_secret_ids,
        )
        return True
    except WrongRedis as exc:
        # Distinct from a connection failure: here a Redis answered, and
        # writing to it would have looked like success while the API
        # carried on serving the client.
        typer.echo(f"ERROR: {exc}", err=True)
        return False
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        log.error("revocation_not_propagated", client_id=client_id, error=str(exc))
        return False


@client_app.command("create")
def client_create(
    name: Annotated[str, typer.Option(help="Human-readable client name")],
    owner_email: Annotated[str, typer.Option(help="Contact address for the owner")],
    plan: Annotated[str, typer.Option(help="Plan name from api_plans")] = "free",
    scope: Annotated[
        list[str] | None, typer.Option(help="Scope; repeat for several")
    ] = None,
) -> None:
    """Creates a client and prints its secret ONCE."""
    from yfin.api.storage import clients as repo

    scopes = scope or []
    unknown = sorted(set(scopes) - set(_scope_values()))
    if unknown:
        typer.echo(f"unknown scope: {', '.join(unknown)}", err=True)
        typer.echo(f"valid scopes: {', '.join(_scope_values())}", err=True)
        raise typer.Exit(EXIT_REJECTED)

    factory = _session_factory()
    with factory() as session:
        try:
            created = repo.create_client(
                session, name=name, owner_email=owner_email, plan=plan, scopes=scopes
            )
        except repo.UnknownPlan:
            typer.echo(f"unknown plan: {plan}", err=True)
            raise typer.Exit(EXIT_REJECTED) from None
        session.commit()

    typer.echo(f"client_id     : {created.client_id}")
    typer.echo(f"client_secret : {created.secret}")
    typer.echo("")
    typer.echo("The secret is shown ONCE and never stored. If it is lost, rotate.")


@client_app.command("list")
def client_list() -> None:
    from yfin.api.storage import clients as repo

    factory = _session_factory()
    with factory() as session:
        rows = repo.list_clients(session)
        for row in rows:
            scopes = ", ".join(repo.scopes_of(session, row.client_id)) or "-"
            state = "active" if row.is_active else "disabled"
            # Ids, not a count: `client revoke` takes one, and a count
            # tells an operator that there is something to revoke without
            # telling them what to type.
            live = ",".join(str(s.id) for s in repo.live_secrets(session, row.client_id))
            typer.echo(
                f"{row.client_id}  {row.plan:<6} {state:<8} "
                f"secrets=[{live}] epoch={row.auth_epoch}  {row.name}  [{scopes}]"
            )
        if not rows:
            typer.echo("no clients registered")


@client_app.command("rotate")
def client_rotate(
    client_id: Annotated[str, typer.Argument(help="Client id to rotate")],
) -> None:
    """Issues a new secret; the old one keeps working for a grace period."""
    from yfin.api.storage import clients as repo

    factory = _session_factory()
    with factory() as session:
        try:
            secret = repo.rotate_secret(session, client_id)
        except repo.TooManyLiveSecrets:
            typer.echo(
                "this client already has two valid secrets; revoke the old one first",
                err=True,
            )
            raise typer.Exit(EXIT_REJECTED) from None
        except repo.UnknownClient:
            typer.echo(f"unknown client: {client_id}", err=True)
            raise typer.Exit(EXIT_REJECTED) from None
        epoch = repo.epoch_of(session, client_id)
        session.commit()

    typer.echo(f"client_secret : {secret}")
    if not _propagate(client_id, epoch):
        typer.echo(
            "WARNING: the rotation is committed but was not propagated to Redis; "
            "existing tokens may stay valid for up to one token lifetime.",
            err=True,
        )
        raise typer.Exit(EXIT_NOT_PROPAGATED)


@client_app.command("revoke")
def client_revoke(
    client_id: Annotated[str, typer.Argument(help="Client id the secret belongs to")],
    secret_id: Annotated[int, typer.Argument(help="Secret id, from `client list`")],
) -> None:
    """Kills ONE secret now, leaving the client and its other secret alive.

    The gap this fills: `rotate` refuses a third live secret and says
    "revoke the old one first", and this module's own documentation
    described a `revoke` command -- but there was none, so the only way to
    cut off a leaked secret was to disable the whole client, which stops
    the traffic that is still legitimate.
    """
    from yfin.api.storage import clients as repo

    factory = _session_factory()
    with factory() as session:
        try:
            repo.revoke_secret(session, client_id, secret_id)
        except repo.UnknownClient:
            typer.echo(f"unknown client or secret: {client_id}/{secret_id}", err=True)
            raise typer.Exit(EXIT_REJECTED) from None
        epoch = repo.epoch_of(session, client_id)
        session.commit()

    typer.echo(f"{client_id} secret {secret_id} revoked")
    # The secret id is published as well as the epoch: the epoch alone
    # stops every token this client holds, which is more than was asked
    # for. The per-secret key is what lets the other secret's tokens live.
    if not _propagate(client_id, epoch, revoked_secret_ids=(secret_id,)):
        typer.echo(
            "WARNING: the secret is revoked in the database but this was not "
            "propagated to Redis; tokens minted with it may stay valid for up "
            "to one token lifetime.",
            err=True,
        )
        raise typer.Exit(EXIT_NOT_PROPAGATED)


@client_app.command("set-scopes")
def client_set_scopes(
    client_id: Annotated[str, typer.Argument(help="Client id to change")],
    scopes: Annotated[
        list[str],
        typer.Argument(help=f"The complete new scope list. One of: {', '.join(_scope_values())}"),
    ],
) -> None:
    """REPLACES the client's scopes; it does not add to them.

    Replacement rather than add/remove because narrowing is the case that
    matters, and an operator who has to think in deltas will eventually
    leave a scope behind.
    """
    from yfin.api.storage import clients as repo

    unknown = sorted(set(scopes) - set(_scope_values()))
    if unknown:
        typer.echo(f"unknown scope: {', '.join(unknown)}", err=True)
        raise typer.Exit(EXIT_REJECTED)

    factory = _session_factory()
    with factory() as session:
        try:
            repo.set_scopes(session, client_id, scopes)
        except repo.UnknownClient:
            typer.echo(f"unknown client: {client_id}", err=True)
            raise typer.Exit(EXIT_REJECTED) from None
        epoch = repo.epoch_of(session, client_id)
        session.commit()

    typer.echo(f"{client_id} scopes: {', '.join(scopes)}")
    _warn_if_not_propagated(client_id, epoch, "the new scopes")


@client_app.command("set-plan")
def client_set_plan(
    client_id: Annotated[str, typer.Argument(help="Client id to change")],
    plan: Annotated[str, typer.Argument(help="Plan name, from the api_plans table")],
) -> None:
    """Moves the client to another plan: rate, quota, page size, concurrency."""
    from yfin.api.storage import clients as repo

    factory = _session_factory()
    with factory() as session:
        try:
            repo.set_plan(session, client_id, plan)
        except repo.UnknownPlan:
            typer.echo(f"unknown plan: {plan}", err=True)
            raise typer.Exit(EXIT_REJECTED) from None
        except repo.UnknownClient:
            typer.echo(f"unknown client: {client_id}", err=True)
            raise typer.Exit(EXIT_REJECTED) from None
        epoch = repo.epoch_of(session, client_id)
        session.commit()

    typer.echo(f"{client_id} plan: {plan}")
    _warn_if_not_propagated(client_id, epoch, "the new plan")


def _warn_if_not_propagated(client_id: str, epoch: int, what: str) -> None:
    """Both changes have to bite before the current token expires.

    A widened scope is harmless if it lags; a NARROWED one is not, and
    neither is a downgraded plan -- the client would keep the old limits
    for the life of an already-issued token. So this exits non-zero for
    the same reason `disable` does.
    """
    if _propagate(client_id, epoch):
        return
    typer.echo(
        f"WARNING: {what} are committed but were not propagated to Redis; "
        "existing tokens may keep the old ones for up to one token lifetime.",
        err=True,
    )
    raise typer.Exit(EXIT_NOT_PROPAGATED)


@client_app.command("disable")
def client_disable(
    client_id: Annotated[str, typer.Argument(help="Client id to disable")],
) -> None:
    _set_active(client_id, active=False)


@client_app.command("enable")
def client_enable(
    client_id: Annotated[str, typer.Argument(help="Client id to re-enable")],
) -> None:
    _set_active(client_id, active=True)


def _set_active(client_id: str, *, active: bool) -> None:
    from yfin.api.storage import clients as repo

    factory = _session_factory()
    with factory() as session:
        try:
            repo.set_active(session, client_id, active)
        except repo.UnknownClient:
            typer.echo(f"unknown client: {client_id}", err=True)
            raise typer.Exit(EXIT_REJECTED) from None
        epoch = repo.epoch_of(session, client_id)
        session.commit()

    word = "enabled" if active else "disabled"
    typer.echo(f"{client_id} {word}")
    if not _propagate(client_id, epoch, disabled=not active):
        typer.echo(
            f"WARNING: the client is {word} in the database but this was not "
            "propagated to Redis; existing tokens may stay valid for up to "
            "one token lifetime.",
            err=True,
        )
        raise typer.Exit(EXIT_NOT_PROPAGATED)


@usage_app.command("flush")
def usage_flush(
    include_today: Annotated[
        bool,
        typer.Option(
            "--include-today",
            help="Also flush today's bucket (races with in-flight requests)",
        ),
    ] = False,
) -> None:
    """Moves buffered request counters into api_usage_daily.

    Meant to run on a schedule. Today's bucket is skipped by default: it
    is still being written to, and a flush that took it would lose
    whatever landed between the read and the delete.
    """
    from yfin.api.core.config import get_api_settings
    from yfin.api.ratelimit import usage

    settings = get_api_settings()
    factory = _session_factory()
    with factory() as session:
        result = usage.flush(session, settings, include_today=include_today)
        session.commit()

    typer.echo(
        f"{result.usage_rows} usage rows, {result.last_used_rows} last-used stamps"
        f" over {len(result.days)} day(s)"
    )
