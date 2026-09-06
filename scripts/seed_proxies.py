"""Proxy havuzunu toplu doldurur.

Kullanim:
    python scripts/seed_proxies.py [DOSYA] [--scheme http] [--prefix p]
    cat liste.txt | python scripts/seed_proxies.py

Girdi formati satir basina `host:port:kullanici:parola` (kullanici ve
parola istege bagli). Bos satirlar ve '#' ile baslayanlar atlanir.

Parolalar DB'ye DUZ METIN YAZILMAZ: YF_PROXY_SECRET_KEY altinda Fernet
ile sifrelenir. Anahtar tanimli degilse betik ACIK hata verir - sessizce
parolasiz kayit olusturmaz, cunku o proxy'ler calismaz.

Idempotenttir: (scheme, host, port, username) demeti zaten varsa satir
atlanir. Girdi dosyasi kimlik bilgisi tasidigi icin depoya KONMAMALIDIR.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from yfin.config import get_settings
from yfin.db import create_db_engine
from yfin.logging_setup import configure_logging
from yfin.models import Proxy, ProxyScheme
from yfin.proxy import ProxyEndpoint, SecretKeyMissing, encrypt_password


def parse_line(line: str, scheme: ProxyScheme) -> ProxyEndpoint | None:
    """`host:port[:user[:pass]]` -> ProxyEndpoint. Yorum/bos satir -> None."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError(f"gecersiz satir (host:port bekleniyor): {text[:24]}...")
    # .lower() SART: hostname'ler buyuk/kucuk harf duyarsizdir (RFC 4343)
    # ve `uq_proxies_endpoint` buna dayanir. MySQL bunu SEMADA sagliyordu
    # (ascii_general_ci); PostgreSQL'de kolon COLLATE "C"dir, yani
    # duyarsizlik YAZMA YOLUNDA saglanmali -- aksi halde
    # HOST.example.com ve host.example.com AYRI iki proxy olur
    # (PG S2.5.2). `parse_dsn` yolunda urlsplit bunu zaten yapiyor.
    host, port = parts[0].strip().lower(), parts[1].strip()
    if not port.isdigit():
        raise ValueError(f"gecersiz port: {port!r}")
    return ProxyEndpoint(
        scheme=scheme,
        host=host,
        port=int(port),
        username=parts[2].strip() if len(parts) > 2 else "",
        # Parolada ':' bulunabilir; kalan parcalar geri birlestirilir
        password=":".join(parts[3:]).strip() if len(parts) > 3 else "",
    )


def parse_lines(lines: Iterable[str], scheme: ProxyScheme) -> Iterator[ProxyEndpoint]:
    for number, line in enumerate(lines, start=1):
        try:
            endpoint = parse_line(line, scheme)
        except ValueError as exc:
            # Mesaj parolayi TASIMAZ: yalnizca satir numarasi ve sebep
            raise ValueError(f"satir {number}: {exc}") from None
        if endpoint is not None:
            yield endpoint


def _label(endpoint: ProxyEndpoint, prefix: str, index: int) -> str:
    return f"{prefix}-{index:02d}"


def seed(session: Session, endpoints: list[ProxyEndpoint], *, prefix: str) -> tuple[int, int]:
    settings = get_settings()
    added = skipped = 0
    for index, endpoint in enumerate(endpoints, start=1):
        exists = session.execute(
            select(Proxy.id).where(
                Proxy.scheme == endpoint.scheme,
                Proxy.host == endpoint.host,
                Proxy.port == endpoint.port,
                Proxy.username == endpoint.username,
            )
        ).scalar_one_or_none()
        if exists is not None:
            skipped += 1
            continue
        session.add(
            Proxy(
                label=_label(endpoint, prefix, index),
                scheme=endpoint.scheme,
                host=endpoint.host,
                port=endpoint.port,
                username=endpoint.username,
                password_enc=encrypt_password(endpoint.password, settings),
            )
        )
        added += 1
    session.commit()
    return added, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="girdi dosyasi (yoksa stdin)")
    parser.add_argument(
        "--scheme",
        default=ProxyScheme.HTTP.value,
        choices=[s.value for s in ProxyScheme],
        help="tum satirlara uygulanacak sema",
    )
    parser.add_argument("--prefix", default="px", help="label oneki")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    with contextlib.ExitStack() as stack:
        source: Iterable[str] = (
            stack.enter_context(open(args.path, encoding="utf-8")) if args.path else sys.stdin
        )
        endpoints = list(parse_lines(source, ProxyScheme(args.scheme)))

    if not endpoints:
        print("girdi bos", file=sys.stderr)
        return 1

    factory = sessionmaker(bind=create_db_engine(settings), expire_on_commit=False, future=True)
    try:
        with factory() as session:
            added, skipped = seed(session, endpoints, prefix=args.prefix)
    except SecretKeyMissing as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"eklendi: {added}  zaten vardi: {skipped}  toplam girdi: {len(endpoints)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
