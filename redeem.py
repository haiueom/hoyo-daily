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


async def redeem_single(client, code, uid):
    try:
        await client.redeem_code(code, uid=uid)
        return "✅"
    except genshin.RedemptionClaimed:
        return "🟡"
    except genshin.RedemptionInvalid:
        return "☠"
    except genshin.RedemptionCooldown:
        return "⏱"
    except genshin.RedemptionException as e:
        # Menangkap error spesifik redemption lainnya
        log.debug(f"Redemption Error ({code}): {e}")
        return "❌"
    except Exception as e:
        log.debug(f"Unknown Error ({code}): {e}")
        return "ERR"


async def process_game(cookies, lang, game, codes, name):
    results = []

    # Kelompokkan tasks agar tidak spam request sekaligus
    # Batch processing: proses 1 kode untuk semua akun, lalu jeda

    for code in codes:
        code_tasks = []
        for cookie in cookies:
            client, _ = await create_genshin_client(cookie, lang, game)
            if not client:
                results.append(
                    RedeemInfo(env_name=cookie.env_name, code=code, status="Cookie Err")
                )
                continue

            try:
                accs = await client.get_game_accounts()
                target = next((a for a in accs if a.game == game), None)
                if not target:
                    continue

                # Buat task untuk redeem
                task = redeem_single(client, code, target.uid)
                code_tasks.append((cookie.env_name, code, task))
            except Exception:
                pass

        # Eksekusi semua akun untuk 1 kode ini
        if code_tasks:
            # Unpack task
            env_names, codes_list, tasks = zip(*code_tasks, strict=True)
            statuses = await asyncio.gather(*tasks)

            for env, c, s in zip(env_names, codes_list, statuses, strict=True):
                # Hanya masukkan ke result jika sukses atau cooldown/invalid
                # Ignore jika error tak dikenal agar report tidak penuh sampah
                results.append(
                    RedeemInfo(
                        uid="*", code=c, status=s, success=(s == "✅"), env_name=env
                    )
                )

        # Jeda antar kode untuk menghindari rate limit IP
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
        help="Paksa cek ulang kode yang sudah 'used' (Gunakan jika ada akun baru)",
    )
    parser.add_argument("-gi", nargs="*", default=[])
    parser.add_argument("-sr", nargs="*", default=[])
    parser.add_argument("-zz", nargs="*", default=[])
    args = parser.parse_args()

    # 1. Kumpulkan kode manual
    codes_map = {"gi": set(args.gi), "sr": set(args.sr), "zz": set(args.zz)}

    # 2. Logic Auto Fetch
    if args.auto:
        active = get_active_codes()

        # Jika --force aktif, kita anggap used list kosong (ignore history)
        if args.force:
            log.info(
                "[FORCE] Mengabaikan history used codes. Semua kode aktif akan dicoba."
            )
            used = {k: set() for k in active}
        else:
            used = get_used_codes()

        # Gabungkan kode: (Manual + (Active - Used))
        for k in codes_map:
            new_codes = set(active.get(k, [])) - used.get(k, set())
            codes_map[k].update(new_codes)

    # Filter yang kosong
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

    for key, codes in codes_map.items():
        if not codes:
            continue
        game, name, disabled = config[key]
        if disabled:
            continue

        log.info(f"[{name}] Processing {len(codes)} Codes...")
        res = await process_game(cookies, lang, game, codes, name)

        if res:
            # Tampilkan Tabel
            table = Table(title=f"🎁 {name}")
            table.add_column("Akun")
            table.add_column("Kode")
            table.add_column("Status")

            # Siapkan pesan Discord
            msg = "```\n"
            has_activity = False

            for r in res:
                table.add_row(r.env_name, r.code, r.status)

                # Hanya notif discord jika statusnya penting
                if r.status in ["✅", "☠", "rules", "Cookie Err"]:
                    msg += f"{r.status} {r.code} ({r.env_name})\n"
                    has_activity = True
                elif r.status == "🟡" and args.force:
                    # Jika force mode, info "Already Claimed" mungkin spam, opsional ditampilkan
                    pass

            msg += "```"

            console.print(table)

            # Kirim webhook hanya jika ada aktivitas penting (sukses/invalid)
            # Jangan kirim jika isinya cuma "Already Claimed" semua (biasa terjadi saat --force)
            if has_activity:
                send_discord_embed(
                    settings.DC_WH_CODE, f"Redeem: {name}", msg, "00ffff"
                )

            # Selalu update used codes agar run berikutnya (tanpa force) lebih cepat
            update_used_codes(key, codes)


if __name__ == "__main__":
    asyncio.run(main())
