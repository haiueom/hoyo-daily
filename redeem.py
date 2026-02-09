import argparse
import asyncio

import genshin
from rich.table import Table

from utils import (
    RedeemInfo,
    check_lang,
    console,
    create_genshin_client,
    fix_asyncio_windows_error,
    get_active_codes,
    get_cookies_from_api,
    get_used_codes,
    log,
    send_discord_embed,
    settings,
    update_used_codes,
)


async def redeem_process(semaphore, cookie, lang, game, code):
    """Worker tunggal: Membuat client lalu redeem, dibatasi oleh semaphore."""
    async with semaphore:
        # 1. Buat Client
        client, err = await create_genshin_client(cookie, lang, game)
        if not client:
            return RedeemInfo(env_name=cookie.env_name, code=code, status="Cookie Err")

        try:
            # 2. Ambil Info Akun
            accs = await client.get_game_accounts()
            target = next((a for a in accs if a.game == game), None)

            if not target:
                return RedeemInfo(env_name=cookie.env_name, code=code, status="No Game")

            # 3. Eksekusi Redeem
            try:
                await client.redeem_code(code, uid=target.uid)
                status = "✅"
            except genshin.RedemptionClaimed:
                status = "🟡"
            except genshin.RedemptionInvalid:
                status = "☠"
            except genshin.RedemptionCooldown:
                status = "⏱"
            except genshin.RedemptionException as e:
                log.debug(f"Redeem Error ({cookie.env_name}): {e}")
                status = "❌"

            return RedeemInfo(
                uid=target.uid,
                code=code,
                status=status,
                success=(status == "✅"),
                env_name=cookie.env_name,
            )

        except Exception as e:
            log.debug(f"Account Error ({cookie.env_name}): {e}")
            return RedeemInfo(env_name=cookie.env_name, code=code, status="ERR")


async def process_game(cookies, lang, game, codes, name):
    results = []
    # Gunakan Semaphore dari settings.MAX_PARALLEL
    semaphore = asyncio.Semaphore(settings.MAX_PARALLEL)

    for code in codes:
        # Buat task untuk setiap akun, tapi jalannya dibatasi semaphore
        tasks = [
            redeem_process(semaphore, cookie, lang, game, code) for cookie in cookies
        ]

        # Jalankan semua akun (terbatasi) untuk 1 kode ini
        code_results = await asyncio.gather(*tasks)

        # Filter hasil yang valid/penting
        valid_results = [r for r in code_results if r.status != "No Game"]
        results.extend(valid_results)

        # Jeda antar KODE (bukan antar akun) untuk keamanan tambahan
        if len(codes) > 1:
            await asyncio.sleep(5)

    return results


async def main():
    fix_asyncio_windows_error()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-a", "--auto", action="store_true", help="Ambil kode aktif dari repo"
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Paksa cek ulang kode yang sudah 'used'",
    )
    parser.add_argument("-gi", nargs="*", default=[])
    parser.add_argument("-sr", nargs="*", default=[])
    parser.add_argument("-zz", nargs="*", default=[])
    args = parser.parse_args()

    codes_map = {"gi": set(args.gi), "sr": set(args.sr), "zz": set(args.zz)}

    if args.auto:
        active = get_active_codes()
        if args.force:
            log.info("[FORCE] Mengabaikan history used codes.")
            used = {k: set() for k in active}
        else:
            used = get_used_codes()

        for k in codes_map:
            new_codes = set(active.get(k, [])) - used.get(k, set())
            codes_map[k].update(new_codes)

    codes_map = {k: list(v) for k, v in codes_map.items() if v}

    if not any(codes_map.values()):
        log.info("Tidak ada kode baru untuk di-redeem.")
        return

    cookies = get_cookies_from_api()
    if not cookies:
        return log.error("No Cookies.")

    config = {
        "gi": (genshin.Game.GENSHIN, "Genshin", settings.NO_GENSHIN),
        "sr": (genshin.Game.STARRAIL, "Star Rail", settings.NO_STARRAIL),
        "zz": (genshin.Game.ZZZ, "ZZZ", settings.NO_ZZZ),
    }

    lang = check_lang(settings.LOCALE)

    log.info(f"🚀 Starting Redeem (Max Parallel: {settings.MAX_PARALLEL})")

    for key, codes in codes_map.items():
        if not codes:
            continue
        game, name, disabled = config[key]
        if disabled:
            continue

        log.info(f"[{name}] Processing {len(codes)} Codes...")
        res = await process_game(cookies, lang, game, codes, name)

        if res:
            table = Table(title=f"🎁 {name}")
            table.add_column("Akun")
            table.add_column("Kode")
            table.add_column("Status")

            msg = "```\n"
            has_activity = False

            for r in res:
                table.add_row(r.env_name, r.code, r.status)
                if r.status in ["✅", "☠", "rules", "Cookie Err", "ERR"]:
                    msg += f"{r.status} {r.code} ({r.env_name})\n"
                    has_activity = True

            msg += "```"
            console.print(table)

            if has_activity:
                send_discord_embed(
                    settings.DC_WH_CODE, f"Redeem: {name}", msg, "00ffff"
                )

            update_used_codes(key, codes)


if __name__ == "__main__":
    asyncio.run(main())
