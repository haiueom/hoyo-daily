import asyncio
from datetime import datetime

import genshin
from rich.console import Group
from rich.panel import Panel
from rich.table import Table

from utils import (
    CookieInfo,
    DailyInfo,
    censor_uid,
    check_lang,
    console,
    create_genshin_client,
    fix_asyncio_windows_error,
    get_cookies_from_api,
    get_days_of_month,
    log,
    send_discord_embed,
    settings,
)


class DailyClaimer:
    def __init__(self, game: genshin.Game):
        self.game = game
        self._monthly_rewards = []

    async def claim(self, cookie: CookieInfo, lang: str) -> DailyInfo:
        parts = cookie.env_name.split("_", 1)
        display_name = parts[1] if len(parts) > 1 else cookie.env_name

        info = DailyInfo(env_name=display_name)

        client, err = await create_genshin_client(cookie, lang, self.game)
        if err:
            info.status = "cookie_err"
            return info

        try:
            # 1. Proses Klaim
            try:
                await client.claim_daily_reward(reward=False)
                info.status = "✅"
            except genshin.AlreadyClaimed:
                info.status = "🟡"
            except genshin.GenshinException as e:
                if e.retcode == -10002:
                    info.status = "no_account"
                    return info
                log.warning(f"[{display_name}] Gagal klaim: {e}")
                info.status = "❌"
                return info

            # 2. Info Reward & Hari
            _, day = await client.get_reward_info()

            if not self._monthly_rewards:
                self._monthly_rewards = await client.get_monthly_rewards()

            reward = self._monthly_rewards[day - 1]
            info.reward = f"{reward.name} x{reward.amount}"

            total_days = get_days_of_month()
            info.check_in_count = f"{day} / {total_days}"

            # 3. Info Akun
            accounts = await client.get_game_accounts()
            target = next((a for a in accounts if a.game == self.game), None)

            if target:
                info.uid = censor_uid(target.uid)
                info.success = True
            else:
                info.uid = "Unknown"
                info.success = True

        except Exception as e:
            log.warning(f"[{display_name}] Error Runtime: {e}")
            info.status = "ERR"

        return info


def send_chunked_webhook(webhook_url, title, lines, color):
    """Memecah pesan Discord agar tidak kena limit karakter."""
    MAX_LENGTH = 1900
    current_msg = "```\n"

    for line in lines:
        if len(current_msg) + len(line) > MAX_LENGTH:
            current_msg += "```"
            send_discord_embed(webhook_url, title, current_msg, color)
            current_msg = "```\n" + line + "\n"
        else:
            current_msg += line + "\n"

    if len(current_msg) > 4:  # Lebih dari sekedar ```\n
        current_msg += "```"
        send_discord_embed(webhook_url, title, current_msg, color)


async def main():
    fix_asyncio_windows_error()
    cookies = get_cookies_from_api()
    if not cookies:
        return log.warning("Tidak ada cookie yang ditemukan.")

    lang = check_lang(settings.LOCALE)
    games = {
        "GENSHIN": (genshin.Game.GENSHIN, settings.NO_GENSHIN),
        "STARRAIL": (genshin.Game.STARRAIL, settings.NO_STARRAIL),
        "ZZZ": (genshin.Game.ZZZ, settings.NO_ZZZ),
    }

    results = {}
    async with asyncio.TaskGroup() as tg:
        for name, (game, disabled) in games.items():
            if disabled:
                continue
            claimer = DailyClaimer(game)
            results[name] = [tg.create_task(claimer.claim(c, lang)) for c in cookies]

    rich_output = []
    timestamp = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")

    # Global Set untuk menampung error cookie unik
    global_cookie_errors = set()

    for name, tasks in results.items():
        infos = [t.result() for t in tasks]

        # Table Terminal: Tampilkan SEMUA status (termasuk no_account/cookie_err) agar user tahu di console
        table = Table(title=f"🎮 {name}", expand=True)
        table.add_column("Akun", style="cyan")
        table.add_column("UID", style="dim")
        table.add_column("Hari", justify="center", style="magenta")
        table.add_column("Status", justify="center")
        table.add_column("Reward", style="green", justify="right")

        # List Pesan Discord per Game
        success_lines = []
        error_lines = []

        has_valid_info = False

        for i in infos:
            # Tambahkan ke Terminal (Semua)
            table.add_row(i.env_name, i.uid, i.check_in_count, i.status, i.reward)

            # --- LOGIKA FILTER WEBHOOK ---

            # 1. Skip no_account sepenuhnya dari webhook
            if i.status == "no_account":
                continue

            has_valid_info = True

            # 2. Tangkap Cookie Error (Jangan kirim sekarang)
            if i.status in ["cookie_err", "Cookie Err"]:
                global_cookie_errors.add(i.env_name)
                continue

            # 3. Pisahkan Sukses dan Error Lainnya
            if i.status in ["✅", "🟡"]:
                success_lines.append(
                    f"{i.status} {i.env_name} ({i.uid}): Day {i.check_in_count}"
                )
            else:
                # Error runtime lain (misal timeout, captcha, dll)
                error_lines.append(f"❌ {i.env_name}: {i.status}")

        if has_valid_info:
            rich_output.append(table)

        if settings.DC_WH_DAILY:
            # Kirim Sukses Per Game
            if success_lines:
                send_chunked_webhook(
                    settings.DC_WH_DAILY,
                    f"Daily Check-In - {name}",
                    success_lines,
                    "00ff00",
                )

            # Kirim Error Game Spesifik (Bukan Cookie)
            if error_lines:
                send_chunked_webhook(
                    settings.DC_WH_DAILY,
                    f"⚠️ Daily Error - {name}",
                    error_lines,
                    "ff0000",
                )

    # --- FINAL REPORT: COOKIE ERRORS ---
    # Dikirim sekali saja di akhir, mencakup semua game
    if global_cookie_errors and settings.DC_WH_DAILY:
        err_names = ", ".join(sorted(global_cookie_errors))
        error_msg = [f"❌ Invalid Cookies ({len(global_cookie_errors)}): {err_names}"]
        send_chunked_webhook(
            settings.DC_WH_DAILY, "⚠️ Account Alert", error_msg, "ff0000"
        )

    if rich_output:
        console.print(Panel(Group(*rich_output), title=f"Daily Report - {timestamp}"))
    else:
        console.print("[yellow]Tidak ada aktivitas daily yang valid.[/yellow]")


if __name__ == "__main__":
    asyncio.run(main())
