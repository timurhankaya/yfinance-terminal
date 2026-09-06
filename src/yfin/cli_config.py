"""`yfin config` komut grubu (CFG S6.2).

Komutlar `settings_store`un INCE sarmalayicilaridir. Dogrulama ve yazma
mantigi bilincli olarak burada DEGIL orada durur: gelecek yonetim paneli
bu komutlari cagirmayacak, ayni fonksiyonlari cagiracak. Mantik buraya
gomulseydi panel onu ATLAR ve ham SQL'e duserdi (CFG S7).

`list` / `get` / `schema` DB ERISILEMEZKEN DE COKMEZ: uyari basip
`source=env|default` ile devam ederler. Kurtarma komutu, kurtarmaya
calistigi arizaya kurban gitmemelidir.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from yfin.config import (
    DB_MANAGED_FIELDS,
    SETTING_GROUPS,
    Settings,
    bootstrap_settings,
    settings_schema,
    source_is_env,
)
from yfin.settings_store import (
    SEED_PATH,
    SettingRejected,
    SettingState,
    Source,
    adopt_env_values,
    export_values,
    fetch_rows,
    load_seed_file,
    normalize_key,
    plan_seed,
    serialize,
    set_setting,
    settings_state,
    unset_setting,
    write_all,
)

config_app = typer.Typer(help="DB tabanli yapilandirma (settings tablosu)", no_args_is_help=True)

# Yapilandirma REDDI. `1` (komut hatasi) ile ayrilmasi, bir CI adiminin
# "gecersiz ayar" ile "komut patladi"yi ayirt edebilmesi icindir
# (cli.py'deki PruneDisabledError deseni).
EXIT_REJECTED = 2


def _read_rows_or_warn(settings: Settings) -> dict[str, str] | None:
    """Satirlari okur; DB katmani kapaliysa ya da erisilemezse `None`.

    `None` "DB'ye BAKILMADI" demektir ve `settings_state` bunu etkin
    kaynagi env/default gostererek dogru sekilde yansitir.
    """
    if source_is_env():
        typer.echo(
            "DB katmani KAPALI (YF_SETTINGS_SOURCE=env): asagidaki degerler "
            ".env ve model varsayilanlarindan geliyor. "
            "`set` / `unset` / `seed` yine de DB'ye YAZAR.",
            err=True,
        )
        return None
    try:
        return fetch_rows(settings)
    except Exception as exc:  # noqa: BLE001 - kurtarma komutu cokmemeli
        typer.echo(f"settings tablosu okunamadi ({exc}); env/default ile devam ediliyor.", err=True)
        return None


def _states(settings: Settings) -> dict[str, SettingState]:
    return settings_state(rows=_read_rows_or_warn(settings))


def _defaults() -> dict[str, str]:
    return {item.key: serialize(item.default) for item in settings_schema()}


@config_app.command("list")
def config_list(
    group: Annotated[str | None, typer.Option("--group", help="Yalniz bu grup")] = None,
    changed: Annotated[
        bool, typer.Option("--changed", help="Etkin degeri model varsayilanindan FARKLI olanlar")
    ] = False,
    source: Annotated[
        str | None, typer.Option("--source", help="db | env | default")
    ] = None,
) -> None:
    """DB-yonetimli ayarlarin etkin degeri ve kaynagi.

    8 env-only alan (db_*, yf_proxy_secret_key, log_level) BURADA YER
    ALMAZ: panelden yonetilemezler. `--changed` "satiri var mi"yi degil
    "etkin deger varsayilandan farkli mi"yi sorar -- operatorun sordugu
    soru budur; satir varligi icin `--source db` kullanilir.
    """
    if group is not None and group not in SETTING_GROUPS:
        typer.echo(f"bilinmeyen grup: {group} ({', '.join(SETTING_GROUPS)})", err=True)
        raise typer.Exit(code=EXIT_REJECTED)
    if source is not None and source not in {s.value for s in Source}:
        typer.echo(f"bilinmeyen kaynak: {source} (db | env | default)", err=True)
        raise typer.Exit(code=EXIT_REJECTED)

    settings = bootstrap_settings()
    states = _states(settings)
    groups = {item.key: item.group for item in settings_schema()}
    defaults = _defaults()

    shown = 0
    for key, state in states.items():
        if group is not None and groups[key] != group:
            continue
        if source is not None and state.source.value != source:
            continue
        if changed and state.value == defaults[key]:
            continue
        # `*` = satiri YOK. Isaretlemek zorunludur: goc sonrasi `.env`
        # katmani fiilen bostur, yani satirsiz bir anahtar dogrudan model
        # varsayilanina duser (CFG S5.4).
        mark = " " if state.has_row else "*"
        typer.echo(f"{mark} {key:<34} {state.value:<28} {state.source.value:<8} {groups[key]}")
        shown += 1
    typer.echo(f"-- {shown} ayar ('*' = settings satiri yok)")


@config_app.command("get")
def config_get(key: Annotated[str, typer.Argument(help="Ayar anahtari")]) -> None:
    """Tek bir ayarin etkin degeri ve kaynagi."""
    canonical = normalize_key(key)
    settings = bootstrap_settings()
    states = _states(settings)
    state = states.get(canonical)
    if state is None:
        typer.echo(f"bilinmeyen ya da DB-yonetimli olmayan ayar: {canonical}", err=True)
        raise typer.Exit(code=EXIT_REJECTED)
    mark = "" if state.has_row else "  (settings satiri yok)"
    typer.echo(f"{state.key} = {state.value}  [{state.source.value}]{mark}")


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Ayar anahtari")],
    value: Annotated[str, typer.Argument(help="Deger (metin; pydantic cozer)")],
) -> None:
    """Dogrular ve `settings` satirini yazar.

    Gecersiz deger YAZIM ANINDA reddedilir (cikis 2); hata bir sonraki
    gece cron'unda cikmamalidir.
    """
    try:
        canonical = set_setting(key, value, settings=bootstrap_settings())
    except SettingRejected as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_REJECTED) from None
    typer.echo(f"{canonical} = {value}")


@config_app.command("unset")
def config_unset(key: Annotated[str, typer.Argument(help="Ayar anahtari")]) -> None:
    """Satiri siler; anahtar `.env`e ve model varsayilanina duser.

    Satir yoksa cikis kodu 0 ve bilgilendirme: komut IDEMPOTENTTIR.
    """
    canonical = normalize_key(key)
    if canonical not in DB_MANAGED_FIELDS:
        typer.echo(f"bilinmeyen ya da DB-yonetimli olmayan ayar: {canonical}", err=True)
        raise typer.Exit(code=EXIT_REJECTED)

    settings = bootstrap_settings()
    removed = unset_setting(canonical, settings=settings)
    typer.echo(f"{canonical}: satir silindi" if removed else f"{canonical}: zaten satir yoktu")

    # `settings` DB katmani devre disi kurulmus bir bootstrap'tir, yani
    # `.env` -> varsayilan zincirinin sonucunu tasir: satir silindikten
    # sonra gecerli olacak deger tam olarak budur.
    typer.echo(f"artik gecerli olacak deger: {serialize(getattr(settings, canonical))}")

    # Anahtar seed dosyasindaysa `unset` KALICI DEGILDIR ve bunu
    # soylemek zorundayiz: JSON "bu kurulumun yapilandirmasi"dir, bir
    # sonraki `seed` degeri geri koyar (CFG S5.3).
    try:
        seed = {normalize_key(str(k)) for k in load_seed_file(SEED_PATH)}
    except (SettingRejected, ValueError):
        seed = set()
    if canonical in seed:
        typer.echo(
            f"UYARI: {canonical} {SEED_PATH} dosyasinda tanimli; "
            "bir sonraki `yfin config seed` onu geri koyacak."
        )


@config_app.command("seed")
def config_seed(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Yalniz plani basar")] = False,
    force: Annotated[
        bool, typer.Option("--force", help="JSON'daki anahtarlarin var olan satirlarini EZER")
    ] = False,
    adopt_env: Annotated[
        bool,
        typer.Option("--adopt-env", help="JSON disi, satirsiz anahtarlar icin etkin .env degeri"),
    ] = False,
) -> None:
    """`config/settings.seed.json` dosyasini uygular (CFG S5.2).

    Kapsam YALNIZCA JSON'daki anahtarlardir; `--force` bile JSON DISI bir
    satira dokunmaz -- aksi halde operatorun panelden yaptigi tum
    ezmeleri sessizce silerdi.

    `--dry-run` eksik satir varsa CIKIS KODU 1 doner, boylece CI'da
    "tohum guncel mi" adimi olarak kullanilabilir.
    """
    settings = bootstrap_settings()
    try:
        seed = load_seed_file(SEED_PATH)
    except (SettingRejected, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_REJECTED) from None

    try:
        rows = fetch_rows(settings)
    except Exception as exc:  # noqa: BLE001 - okunamiyorsa yazmayi da denemeyiz
        typer.echo(f"settings tablosu okunamadi: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if rows is None:
        typer.echo("settings tablosu yok; once `yfin db upgrade` calistirin.", err=True)
        raise typer.Exit(code=1)

    try:
        plan = plan_seed(
            seed,
            rows,
            force=force,
            adopt_env=adopt_env_values() if adopt_env else None,
        )
    except SettingRejected as exc:
        # YA HEP YA HIC: gecersiz bir JSON'da HICBIR SEY yazilmaz.
        typer.echo(f"tohum reddedildi, hicbir sey yazilmadi: {exc}", err=True)
        raise typer.Exit(code=EXIT_REJECTED) from None

    for key, value in sorted(plan.items()):
        typer.echo(f"{'[dry-run] ' if dry_run else ''}{key} = {value}")

    if dry_run:
        typer.echo(f"-- {len(plan)} satir yazilacakti")
        raise typer.Exit(code=1 if plan else 0)

    write_all(plan, settings=settings)
    typer.echo(f"-- {len(plan)} satir yazildi")


@config_app.command("export")
def config_export(
    all_keys: Annotated[
        bool, typer.Option("--all", help="DB-yonetimli her degerin tam anlik goruntusu")
    ] = False,
) -> None:
    """HER ZAMAN JSON basar.

    Varsayilan yalnizca DB SATIRI OLANLARI icerir -- yani seed dosyasinin
    dogal tersidir. `--all` model varsayilanlarini da icerir; bu cikti
    `config/settings.seed.json`a YAZILMAMALIDIR (CFG S9): yazilirsa
    varsayilan degisikliklerinin kuruluma yansima bagi kopar. Yedek depo
    DISINA alinir.
    """
    states = _states(bootstrap_settings())
    typer.echo(
        json.dumps(
            export_values(states, all_keys=all_keys), indent=2, sort_keys=True, ensure_ascii=False
        )
    )


@config_app.command("schema")
def config_schema(
    as_json: Annotated[bool, typer.Option("--json", help="Panel bicimi")] = False,
) -> None:
    """Panelin formu cizmesi icin gereken metadata. DB'ye BAKMAZ."""
    note = (
        "Not: 8 env-only alan (db_*, yf_proxy_secret_key, log_level) "
        "burada YER ALMAZ; onlar .env'de kalir."
    )
    items = settings_schema()
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "note": note,
                    "fields": [
                        {
                            "key": i.key,
                            "group": i.group,
                            "type": i.type,
                            "default": i.default,
                            "min": i.min,
                            "max": i.max,
                            "description": i.description,
                        }
                        for i in items
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    typer.echo(note)
    for item in items:
        bounds = ""
        if item.min is not None or item.max is not None:
            bounds = f" [{item.min if item.min is not None else '-'}"
            bounds += f"..{item.max if item.max is not None else '-'}]"
        typer.echo(
            f"{item.key:<34} {item.type:<6} {serialize(item.default):<28} "
            f"{item.group:<12}{bounds} {item.description}"
        )
