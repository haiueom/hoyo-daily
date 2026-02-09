import asyncio
from datetime import datetime
import genshin
from rich.panel import Panel
from rich.table import Table
from rich.console import Group

from utils import (
    CookieInfo, DailyInfo, censor_uid, check_lang, create_genshin_client,
    fix_asyncio_windows_error, get_cookies_from_api, get_days_of_month,
    send_discord_embed, settings, console, log
)

class DailyClaimer:
    def __init__(self, game: genshin.Game):
        self.game = game
        self._monthly_rewards = []

    async def claim(self, cookie: CookieInfo, lang: str) -> DailyInfo:
        # Ambil nama akun dari env_name (misal ACC1_NAME -> NAME)
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
                if e.retcode == -10002: # Karakter belum dibuat/server salah
                    info.status = "no_account"
                    return info
                log.warning(f"[{display_name}] Gagal klaim: {e}")
                info.status = "❌"
                return info

            # 2. Info Reward & Hari Login
            _, day = await client.get_reward_info()

            # Cache reward bulanan agar tidak fetch berulang kali
            if not self._monthly_rewards:
                self._monthly_rewards = await client.get_monthly_rewards()

            reward = self._monthly_rewards[day - 1]
            info.reward = f"{reward.name} x{reward.amount}"

            # >>> MENAMBAHKAN INFO HARI <<<
            total_days = get_days_of_month()
            info.check_in_count = f"{day}/{total_days}"

            # 3. Info Akun (UID, Nickname)
            accounts = await client.get_game_accounts()
            target = next((a for a in accounts if a.game == self.game), None)

            # Jika tidak ketemu di get_game_accounts, coba cari manual atau skip
            if target:
                info.uid = censor_uid(target.uid)
                info.success = True
            else:
                # Fallback jika akun ada tapi tidak terdeteksi di region default
                info.uid = "Unknown"
                info.success = True

        except Exception as e:
            log.warning(f"[{display_name}] Error Runtime: {e}")
            info.status = "ERR"

        return info

async def main():
    fix_asyncio_windows_error()
    cookies = get_cookies_from_api()
    if not cookies:
        return log.warning("Tidak ada cookie yang ditemukan. Cek konfigurasi .env")

    lang = check_lang(settings.LOCALE)

    # Konfigurasi Game
    games = {
        "GENSHIN": (genshin.Game.GENSHIN, settings.NO_GENSHIN),
        "STARRAIL": (genshin.Game.STARRAIL, settings.NO_STARRAIL),
        "ZZZ": (genshin.Game.ZZZ, settings.NO_ZZZ),
    }

    results = {}

    # Eksekusi Paralel menggunakan TaskGroup
    async with asyncio.TaskGroup() as tg:
        for name, (game, disabled) in games.items():
            if disabled: continue

            claimer = DailyClaimer(game)
            # Buat list task untuk setiap cookie per game
            results[name] = [tg.create_task(claimer.claim(c, lang)) for c in cookies]

    # Menampilkan Hasil
    rich_output = []
    timestamp = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")

    for name, tasks in results.items():
        infos = [t.result() for t in tasks]

        # Hanya tampilkan tabel jika ada setidaknya satu akun yang sukses/terdeteksi
        valid_infos = [i for i in infos if i.status != "no_account"]
        if not valid_infos: continue

        table = Table(title=f"🎮 {name}", expand=True)
        table.add_column("Akun", style="cyan")
        table.add_column("UID", style="dim")
        table.add_column("Hari", justify="center", style="magenta") # Kolom Hari
        table.add_column("Status", justify="center")
        table.add_column("Reward", style="green", justify="right")

        msg = "```\n"
        for i in valid_infos:
            # Tambahkan baris ke tabel terminal
            table.add_row(i.env_name, i.uid, i.check_in_count, i.status, i.reward)

            # Format pesan Discord
            if i.status in ["✅", "🟡"]:
                msg += f"{i.status} {i.env_name} (Hari {i.check_in_count}): {i.reward}\n"
            else:
                msg += f"{i.status} {i.env_name}: {i.status}\n"

        msg += "```"

        rich_output.append(table)

        # Kirim Webhook
        if settings.DC_WH_DAILY:
            send_discord_embed(
                settings.DC_WH_DAILY,
                f"Daily Check-In - {name}",
                msg,
                color="00ff00"
            )

    if rich_output:
        console.print(Panel(Group(*rich_output), title=f"Daily Report - {timestamp}"))
    else:
        console.print("[yellow]Tidak ada aktivitas daily yang berhasil dijalankan.[/yellow]")

if __name__ == "__main__":
    asyncio.run(main())
