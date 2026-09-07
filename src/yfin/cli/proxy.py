"""`yfin proxy` -- the proxy pool."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Annotated, Any

import typer

from yfin.cli.common import session_factory
from yfin.core.logging_setup import get_logger

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from yfin.models import Proxy

log = get_logger(__name__)

proxy_app = typer.Typer(help="Proxy pool management", no_args_is_help=True)


def _proxy_by_label(session: Session, label: str) -> Proxy:
    from sqlalchemy import select

    from yfin.models import Proxy

    row = session.execute(select(Proxy).where(Proxy.label == label)).scalar_one_or_none()
    if row is None:
        typer.echo(f"proxy not found: {label}", err=True)
        raise typer.Exit(code=1)
    return row


@proxy_app.command("add")
def proxy_add(
    url: Annotated[str, typer.Argument(help="scheme://[user:pass@]host:port")],
    label: Annotated[str | None, typer.Option("--label")] = None,
) -> None:
    """Adds a proxy to the pool. The password is stored encrypted with Fernet."""
    # The models package and the proxy helpers are imported here, not at
    # module level: this module is imported by `cli/app.py` to mount the
    # group, so a module-level import would put SQLAlchemy's whole model
    # package behind `yfin --help`.
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from yfin import proxy as px
    from yfin.core.config import get_settings
    from yfin.core.logging_setup import configure_logging
    from yfin.models import Proxy, ProxyScheme

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        endpoint = px.parse_dsn(url)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    if endpoint.scheme is ProxyScheme.HTTPS:
        # curl_cffi emits a CurlCffiWarning for this scheme; most users actually
        # want http:// for a CONNECT tunnel.
        typer.echo("warning: 'https' means TLS to the proxy; use 'http' for a CONNECT tunnel")

    try:
        password_enc = px.encrypt_password(endpoint.password, settings)
    except px.SecretKeyMissing as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None

    name = label or px.default_label(endpoint)
    factory = session_factory()
    with factory() as session:
        existing = session.execute(
            select(Proxy).where(
                Proxy.scheme == endpoint.scheme,
                Proxy.host == endpoint.host,
                Proxy.port == endpoint.port,
                Proxy.username == endpoint.username,
            )
        ).scalar_one_or_none()
        if existing is not None:
            typer.echo(f"already present: {existing.label}")
            return
        session.add(
            Proxy(
                label=name,
                scheme=endpoint.scheme,
                host=endpoint.host,
                port=endpoint.port,
                username=endpoint.username,
                password_enc=password_enc,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # `label` is UNIQUE. The existence check above looks at the endpoint,
            # not the label: adding a different endpoint under the same label
            # used to print a raw SQLAlchemy traceback.
            session.rollback()
            typer.echo(f"that label is already in use: {name}", err=True)
            raise typer.Exit(code=1) from None
    typer.echo(f"added: {name} ({endpoint.scheme.value}://{endpoint.host}:{endpoint.port})")


@proxy_app.command("list")
def proxy_list(
    show_all: Annotated[bool, typer.Option("--all", help="Also show disabled proxies")] = False,
) -> None:
    """Lists the pool. The password is never printed."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from yfin.models import Proxy, ProxyHealth

    factory = session_factory()
    now = datetime.now(UTC)
    stmt = select(Proxy).order_by(Proxy.label)
    if not show_all:
        stmt = stmt.where(Proxy.is_enabled.is_(True))
    with factory() as session:
        rows = list(session.execute(stmt).scalars())
    if not rows:
        typer.echo("no proxies")
        return
    typer.echo(f"{'LABEL':<20} {'ENDPOINT':<28} {'ON':<3} {'HEALTH':<18} {'OK/FAIL':<12} LATENCY")
    for row in rows:
        health = row.health.value
        if row.health is ProxyHealth.COOLDOWN and row.cooldown_until and row.cooldown_until <= now:
            # Don't let the stale enum mislead the operator: the cooldown has
            # expired and the proxy is eligible again, but health stays
            # 'cooldown' until the next success.
            health = "cooldown (expired)"
        latency = f"{row.last_latency_ms} ms" if row.last_latency_ms is not None else "-"
        typer.echo(
            f"{row.label:<20} {f'{row.scheme.value}://{row.endpoint()}':<28} "
            f"{'1' if row.is_enabled else '0':<3} {health:<18} "
            f"{f'{row.success_count}/{row.failure_count}':<12} {latency}"
        )


@proxy_app.command("enable")
def proxy_enable(label: str) -> None:
    """Operator decision: re-add to the pool (does not touch health)."""
    _set_enabled(label, True)


@proxy_app.command("disable")
def proxy_disable(label: str) -> None:
    """Operator decision: remove from the pool (does not touch health)."""
    _set_enabled(label, False)


def _set_enabled(label: str, value: bool) -> None:
    from sqlalchemy import update

    from yfin.models import Proxy

    factory = session_factory()
    with factory() as session:
        row = _proxy_by_label(session, label)
        session.execute(update(Proxy).where(Proxy.id == row.id).values(is_enabled=value))
        session.commit()
    typer.echo(f"{'etkin' if value else 'devre disi'}: {label}")


@proxy_app.command("reset")
def proxy_reset(label: str) -> None:
    """Resets health state; brings a dead proxy back.

    Cumulative success_count/failure_count are preserved: past data is not
    deleted.
    """
    from sqlalchemy import update

    from yfin.models import Proxy, ProxyHealth

    factory = session_factory()
    with factory() as session:
        row = _proxy_by_label(session, label)
        session.execute(
            update(Proxy)
            .where(Proxy.id == row.id)
            .values(
                health=ProxyHealth.UNKNOWN,
                consecutive_failures=0,
                cooldown_rounds=0,
                cooldown_until=None,
                last_error=None,
            )
        )
        session.commit()
    typer.echo(f"sifirlandi: {label}")


@proxy_app.command("remove")
def proxy_remove(label: str) -> None:
    """Deletes from the pool. The copy in sync_run_items.proxy_label remains."""
    from sqlalchemy import delete

    from yfin.models import Proxy

    factory = session_factory()
    with factory() as session:
        row = _proxy_by_label(session, label)
        session.execute(delete(Proxy).where(Proxy.id == row.id))
        session.commit()
    typer.echo(f"deleted: {label}")


@proxy_app.command("check")
def proxy_check(
    label: Annotated[str | None, typer.Option("--label", help="Only this proxy")] = None,
) -> None:
    """Verifies each proxy against Yahoo and refreshes its health.

    Does not use yfinance: yf.config is process-global, so checking N proxies
    in parallel would need N processes. A raw curl_cffi request is sent
    instead. Two endpoints are tried: chart does not require a crumb, but
    real traffic also goes through /v1/test/getcrumb.
    """
    from sqlalchemy import select

    from yfin import proxy as px
    from yfin.core.config import get_settings
    from yfin.core.logging_setup import configure_logging
    from yfin.models import Proxy

    settings = get_settings()
    configure_logging(settings.log_level)
    policy = px.ProxyPolicy.from_settings(settings)
    factory = session_factory()

    stmt = select(Proxy).order_by(Proxy.label)
    if label:
        stmt = stmt.where(Proxy.label == label)
    with factory() as session:
        rows = list(session.execute(stmt).scalars())
        targets: list[tuple[int, str, px.ProxyEndpoint | None, str]] = []
        for row in rows:
            try:
                targets.append((int(row.id), row.label, px.endpoint_of(row, settings), ""))
            except px.PasswordUndecryptable as exc:
                # State is not changed, to avoid producing a false diagnosis.
                targets.append((int(row.id), row.label, None, str(exc)))

    if not targets:
        typer.echo("no proxies")
        return

    def _run(target: tuple[int, str, px.ProxyEndpoint | None, str]) -> tuple[int, str, Any]:
        proxy_id, name, endpoint, error = target
        if endpoint is None:
            return proxy_id, name, px.CheckResult(name, None, None, error)
        return proxy_id, name, px.check_endpoint(endpoint, settings.yf_proxy_check_timeout)

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        outcomes = list(pool.map(_run, targets))

    with factory() as session:
        for proxy_id, name, result in outcomes:
            if result.event is None:
                typer.echo(f"{name:<20} ATLANDI  {result.detail}")
                continue
            px.persist_event(
                session,
                proxy_id,
                result.event,
                policy=policy,
                error=result.detail or None,
                latency_ms=result.latency_ms,
                checked=True,
            )
            latency = f"{result.latency_ms} ms" if result.latency_ms is not None else "-"
            typer.echo(f"{name:<20} {result.event.value:<14} {latency}  {result.detail}")
        session.commit()
