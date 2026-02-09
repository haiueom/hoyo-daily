import argparse
import asyncio

import genshin
from rich.table import Table

from utils import (
    RedeemInfo,
    censor_uid,
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
    async with semaphore:
        client, err = await create_genshin_client(cookie, lang, game)
        if not client:
            return RedeemInfo(env_name=cookie.env_name, code=code, status="Cookie Err")

        try:
            parts = cookie.env_name.split("_", 1)
            display_name = parts[1] if len(parts) > 1 else cookie.env_name

            accs = await client.get_game_accounts()
            target = next((a for a in accs if a.game == game), None)

            if not target:
                return RedeemInfo(env_name=display_name, code=code, status="No Game")

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
                log.debug(f"Redeem Error ({display_name}): {e}")
                status = "❌"

            return RedeemInfo(
                uid=censor_uid(target.uid),
                code=code,
                status=status,
                success=(status == "✅"),
                env_name=display_name,
            )

        except Exception as e:
            parts = cookie.env_name.split("_", 1)
            display_name = parts[1] if len(parts) > 1 else cookie.env_name
            log.debug(f"Account Error ({display_name}): {e}")
            return RedeemInfo(env_name=display_name, code=code, status="ERR")


async def process_game(cookies, lang, game, codes, name):
    results = []
    semaphore = asyncio.Semaphore(settings.MAX_PARALLEL)

    for code in codes:
        tasks = [
            redeem_process(semaphore, cookie, lang, game, code) for cookie in cookies
        ]
        code_results = await asyncio.gather(*tasks)

        # Terminal tetap butuh info "No Game" untuk debugging, tapi webhook nanti filter
        results.extend(code_results)

        if len(codes) > 1:
            await asyncio.sleep(5)

    return results


def send_chunked_webhook(webhook_url, title, lines, color):
    MAX_LENGTH = 1900
    current_msg = "```\n"
    for line in lines:
        if len(current_msg) + len(line) > MAX_LENGTH:
            current_msg += "```"
            send_discord_embed(webhook_url, title, current_msg, color)
            current_msg = "```\n" + line + "\n"
        else:
            current_msg += line + "\n"
    if len(current_msg) > 4:
        current_msg += "```"
        send_discord_embed(webhook_url, title, current_msg, color)


async def main():
    fix_asyncio_windows_error()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-a", "--auto", action="store_true", help="Ambil kode aktif dari repo"
    )
    parser.add_argument(
        "-f", "--force", action="store_true", help="Paksa cek ulang history"
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

    # Global Set untuk Cookie Error
    global_cookie_errors = set()

    for key, codes in codes_map.items():
        if not codes:
            continue
        game, name, disabled = config[key]
        if disabled:
            continue

        log.info(f"[{name}] Processing {len(codes)} Codes...")
        res = await process_game(cookies, lang, game, codes, name)

        if res:
            table = Table(title=f"🎁 {name}", expand=True)
            table.add_column("Akun", style="cyan")
            table.add_column("UID", style="dim")
            table.add_column("Status", justify="center")
            table.add_column("Kode", justify="center", style="magenta")

            success_lines = []
            error_lines = []

            for r in res:
                # Terminal: Tampilkan semua kecuali "No Game" agar tidak spam
                if r.status != "No Game":
                    table.add_row(r.env_name, r.uid, r.status, r.code)

                # --- FILTER WEBHOOK ---

                # 1. Skip No Game
                if r.status == "No Game":
                    continue

                # 2. Tangkap Cookie Error (Jangan kirim sekarang)
                if r.status in ["Cookie Err", "cookie_err"]:
                    global_cookie_errors.add(r.env_name)
                    continue

                # 3. Sukses / Claimed
                if r.status in ["✅", "🟡"]:
                    success_lines.append(
                        f"{r.status} [{r.code}] {r.env_name} ({r.uid})"
                    )

                # 4. Error Redeem (Cooldown, Invalid, dll)
                elif r.status == "ERR":
                    error_lines.append(f"❌ {r.env_name}: Unknown Error")
                elif r.status in ["☠", "⏱", "rules", "❌"]:
                    error_lines.append(f"{r.status} [{r.code}] {r.env_name}")

            console.print(table)

            if settings.DC_WH_REDEEM:
                if success_lines:
                    send_chunked_webhook(
                        settings.DC_WH_REDEEM,
                        f"Redeem Code - {name}",
                        success_lines,
                        "00ff00",
                    )
                if error_lines:
                    send_chunked_webhook(
                        settings.DC_WH_REDEEM,
                        f"⚠️ Redeem Error - {name}",
                        error_lines,
                        "ff0000",
                    )

            update_used_codes(key, codes)

    # --- FINAL REPORT: COOKIE ERRORS ---
    if global_cookie_errors and settings.DC_WH_REDEEM:
        err_names = ", ".join(sorted(global_cookie_errors))
        error_msg = [f"❌ Invalid Cookies ({len(global_cookie_errors)}): {err_names}"]
        send_chunked_webhook(
            settings.DC_WH_REDEEM, "⚠️ Account Alert", error_msg, "ff0000"
        )


if __name__ == "__main__":
    asyncio.run(main())
