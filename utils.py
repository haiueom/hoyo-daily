import asyncio
import logging
import os
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime  # Dipindahkan ke atas (Fix E402)
from re import sub

import genshin
import requests
from discord_webhook import DiscordEmbed, DiscordWebhook
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.console import Console
from rich.logging import RichHandler

# --- Setup Logging & Console ---
logging.basicConfig(
    level="INFO",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=Console(), rich_tracebacks=True)],
)
log = logging.getLogger("rich")
console = Console()


# --- Configuration Management (Pydantic) ---
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App Config
    LOCALE: str = "en-us"
    MAX_PARALLEL: int = 10

    # Secrets
    SECRET_KEY: str | None = None
    COOKIE_API: str | None = None

    # Webhooks
    DC_WH_DAILY: str = ""
    DC_WH_REDEEM: str = ""

    # Feature Flags
    NO_GENSHIN: bool = False
    NO_STARRAIL: bool = False
    NO_ZZZ: bool = False
    NO_HONKAI: bool = False
    NO_TOT: bool = False


settings = Settings()


# --- Data Structures ---
@dataclass
class CookieInfo:
    env_name: str = ""
    cookies: str | dict = ""

    def get(self) -> str | dict:
        return self.cookies


@dataclass
class DailyInfo:
    uid: str = "❓"
    level: str = "❓"
    name: str = "❓"
    server: str = "❓"
    status: str = "❌"
    check_in_count: str = "❓"
    reward: str = "❓"
    success: bool = False
    env_name: str = "❓"


@dataclass
class RedeemInfo:
    uid: str = "❓"
    level: str = "❓"
    name: str = "❓"
    server: str = "❓"
    code: str = "❓"
    status: str = "❌"
    success: bool = False
    env_name: str = "❓"


# --- Helper Functions ---
def check_lang(lang: str) -> str:
    valid = {
        "zh-cn",
        "zh-tw",
        "de-de",
        "en-us",
        "es-es",
        "fr-fr",
        "id-id",
        "ja-jp",
        "ko-kr",
        "pt-pt",
        "ru-ru",
        "th-th",
        "vi-vn",
    }
    lang = lang.lower()
    if lang not in valid:
        log.warning(f"[LANGUAGE] '{lang}' not supported. Using 'en-us'.")
        return "en-us"
    return lang


def censor_uid(uid: int | str) -> str:
    s = str(uid)
    return s[:-6] + "■■■■■" + s[-1] if len(s) >= 6 else s


def format_name(name: str) -> str:
    # Ganti karakter non-alphanumeric (kecuali awal/akhir) dengan underscore
    name = sub(r"(?<!^)\W+(?!$)", "_", name)
    return name.upper()


def fix_asyncio_windows_error() -> None:
    if sys.version_info >= (3, 8) and sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def get_days_of_month() -> int:
    now = datetime.now()
    return monthrange(now.year, now.month)[1]


# --- Core Logic ---


def get_cookies_from_api() -> list[CookieInfo]:
    """
    Mengambil cookie dari API dengan struktur data:
    interface Account { id: number, name: string, cookie_token: string, account_id: number, ... }
    interface ApiResponse { success: boolean, message: string, data?: Account[], ... }
    """
    if not settings.COOKIE_API or not settings.SECRET_KEY:
        log.error("[COOKIE] COOKIE_API atau SECRET_KEY belum diset di .env")
        return []

    try:
        response = requests.get(
            settings.COOKIE_API,
            headers={"Authorization": f"Bearer {settings.SECRET_KEY}"},
            timeout=15,
        )
        response.raise_for_status()
        resp_json = response.json()

        # Cek flag success dari ApiResponse
        if not resp_json.get("success", False):
            log.error(
                f"[COOKIE] API Error: {resp_json.get('message', 'Unknown error')}"
            )
            return []

        data = resp_json.get("data", [])
    except Exception as e:
        log.error(f"[COOKIE] Gagal mengambil cookie: {e}")
        return []

    cookies = []
    for idx, item in enumerate(data, 1):
        try:
            # Mapping sesuai interface Account
            raw_name = item.get("name", "Unknown")
            safe_name = format_name(raw_name)
            env_name = f"ACC{idx}_{safe_name}"

            # Ambil account_id (number) dan ubah ke string
            acc_id = str(item.get("account_id", ""))
            # Ambil cookie_token (string)
            cookie_token = item.get("cookie_token", "")

            if not acc_id or not cookie_token:
                log.warning(f"[COOKIE] Data tidak lengkap untuk akun {env_name}, skip.")
                continue

            cookie_str = f"account_id_v2={acc_id}; cookie_token_v2={cookie_token}"
            cookies.append(CookieInfo(env_name=env_name, cookies=cookie_str))
        except Exception as e:
            log.warning(f"[COOKIE] Gagal memproses item {idx}: {e}")
            continue

    return sorted(cookies, key=lambda x: x.env_name)


async def create_genshin_client(
    cookie: CookieInfo, lang: str, game: genshin.Game
) -> tuple[genshin.Client | None, str | None]:
    """Factory function untuk membuat client Genshin yang aman."""
    try:
        cookies = await genshin.complete_cookies(cookies=cookie.get())
        client = genshin.Client(cookies=cookies, lang=lang, game=game)
        return client, None
    except Exception as e:
        return None, str(e)


def send_discord_embed(
    webhook_url: str, title: str, msg: str, color: str = "00ff00"
) -> None:
    """Mengirim notifikasi ke Discord (Unified)."""
    if not webhook_url:
        return
    try:
        webhook = DiscordWebhook(url=webhook_url)
        embed = DiscordEmbed(title=title, description=msg, color=color)
        embed.set_timestamp()
        embed.set_footer(text="Hoyo Tools")
        webhook.add_embed(embed)
        webhook.execute()
    except Exception as e:
        log.error(f"[DISCORD] Gagal mengirim webhook: {e}")


# --- Code Logic ---

GITHUB_RAW_URL = "https://github.com/haiueom/hoyo-code/raw/refs/heads/main/"
GAME_MAP = {"genshin": "gi", "starrail": "sr", "zzz": "zz"}


def get_active_codes() -> dict[str, list[str]]:
    active = {v: [] for v in GAME_MAP.values()}
    for path, key in GAME_MAP.items():
        try:
            r = requests.get(f"{GITHUB_RAW_URL}{path}/active.json", timeout=10)
            if r.ok:
                data = r.json()
                if isinstance(data, list):
                    if data and isinstance(data[0], dict):
                        active[key] = [i["code"] for i in data if "code" in i]
                    else:
                        active[key] = data
        except Exception as e:
            log.warning(f"[CODES] Gagal fetch kode {path}: {e}")
    return active


def get_used_codes() -> dict[str, set[str]]:
    used = {v: set() for v in GAME_MAP.values()}
    for path_key, game_key in GAME_MAP.items():
        try:
            if os.path.exists(f"used/{path_key}.txt"):
                with open(f"used/{path_key}.txt", encoding="utf-8") as f:
                    used[game_key] = set(f.read().splitlines())
        except Exception:
            pass
    return used


def update_used_codes(game_key: str, codes: list[str]):
    path_key = next((k for k, v in GAME_MAP.items() if v == game_key), None)
    if not path_key:
        return
    try:
        os.makedirs("used", exist_ok=True)
        with open(f"used/{path_key}.txt", "a", encoding="utf-8") as f:
            for c in codes:
                f.write(f"{c}\n")
    except Exception as e:
        log.error(f"Gagal update used codes: {e}")


def reset_used_files():
    for game_path in GAME_MAP:
        try:
            with open(f"used/{game_path}.txt", "w", encoding="utf-8") as f:
                f.write("")
        except Exception:  # Fix E722 (Bare except)
            pass
