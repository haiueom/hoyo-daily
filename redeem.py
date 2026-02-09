# redeem.py
import argparse
import asyncio
from utils import (
    censor_uid, check_lang, create_genshin_client, fix_asyncio_windows_error,
    get_cookies_from_api, get_active_codes, update_used_codes,
    send_discord_embed, settings, console, log, RedeemInfo
)
import genshin
from rich.table import Table

async def redeem_single(client, code, uid):
    try:
        await client.redeem_code(code, uid=uid)
        return "✅"
    except genshin.RedemptionClaimed: return "🟡"
    except genshin.RedemptionInvalid: return "☠"
    except genshin.RedemptionCooldown: return "⏱"
    except Exception: return "❌"

async def process_game(cookies, lang, game, codes, name):
    results = []
    for code in codes:
        # Proses 1 kode untuk semua akun (Paralel)
        tasks = []
        for cookie in cookies:
            client, _ = await create_genshin_client(cookie, lang, game)
            if not client:
                results.append(RedeemInfo(env_name=cookie.env_name, code=code, status="Cookie Err"))
                continue

            try:
                accs = await client.get_game_accounts()
                target = next((a for a in accs if a.game == game), None)
                if not target: continue

                status = await redeem_single(client, code, target.uid)
                results.append(RedeemInfo(target.uid, code, status, True, cookie.env_name))
            except: pass

        await asyncio.sleep(5) # Delay 5 detik antar kode
    return results

async def main():
    fix_asyncio_windows_error()
    parser = argparse.ArgumentParser()
    parser.add_argument("-a", "--auto", action="store_true", help="Ambil kode aktif dari repo")
    parser.add_argument("-gi", nargs="*", default=[])
    parser.add_argument("-sr", nargs="*", default=[])
    parser.add_argument("-zz", nargs="*", default=[])
    args = parser.parse_args()

    codes_map = {"gi": set(args.gi), "sr": set(args.sr), "zz": set(args.zz)}
    if args.auto:
        active = get_active_codes()
        for k in codes_map: codes_map[k].update(active.get(k, []))

    # Cek file used (sederhana) - bisa dikembangkan lagi
    # Di sini kita redeem saja, system genshin akan tolak jika sudah redeem

    cookies = get_cookies_from_api()
    if not cookies: return log.error("No Cookies.")

    config = {
        "gi": (genshin.Game.GENSHIN, "Genshin", settings.NO_GENSHIN),
        "sr": (genshin.Game.STARRAIL, "Star Rail", settings.NO_STARRAIL),
        "zz": (genshin.Game.ZZZ, "ZZZ", settings.NO_ZZZ),
    }

    for key, codes in codes_map.items():
        if not codes: continue
        game, name, disabled = config[key]
        if disabled: continue

        log.info(f"[{name}] Redeem: {codes}")
        res = await process_game(cookies, check_lang(settings.LOCALE), game, list(codes), name)

        if res:
            table = Table(title=f"🎁 {name}")
            table.add_column("Akun"); table.add_column("Kode"); table.add_column("Status")
            msg = "```\n"
            for r in res:
                table.add_row(r.env_name, r.code, r.status)
                msg += f"{r.status} {r.code} ({r.env_name})\n"
            msg += "```"
            console.print(table)
            send_discord_embed(settings.DC_WH_CODE, f"Redeem: {name}", msg, "00ffff")
            update_used_codes(key, list(codes))

if __name__ == "__main__":
    asyncio.run(main())
