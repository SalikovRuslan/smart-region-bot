"""Telegram-бот: раз на годину публікує новий пост з рейтингом від 1 місця до проєкту №53
(без звуку), а коли змінюється його місце — одразу позачерговий пост зі звуком.
Старі пости не редагуються, тож у каналі зберігається вся історія.

Запуск:          TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python bot.py
Тестовий режим:  python bot.py              (без токена — лише друкує повідомлення)
Дізнатися chat_id: python bot.py --chat-id
"""
import getpass
import html
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from parser import TARGET_ID, project_url, scrape

STATE_FILE = Path(__file__).with_name("state.json")
TOP_N = 10
TITLE_LEN = 40
KYIV = ZoneInfo("Europe/Kyiv")
NO_PREVIEW = {"is_disabled": True}
TELEGRAM_LIMIT = 4096  # максимальна довжина повідомлення
POST_EVERY = timedelta(hours=1)
# GitHub запускає розклад неточно, тому пост виходить, якщо минуло хоча б 50 хв
SCHEDULE_SLACK = timedelta(minutes=10)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


class TelegramError(Exception):
    pass


def tg(method, **params):
    try:
        res = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=15)
        data = res.json()
    except (requests.RequestException, ValueError) as e:
        # Не друкуємо текст винятку: у ньому URL з токеном
        raise TelegramError(f"{method}: {type(e).__name__}") from None
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description')}")
    return data["result"]


# --- Форматування ---

def short(title, max_len=TITLE_LEN):
    return title if len(title) <= max_len else title[: max_len - 1] + "…"


def votes_word(n):
    if n % 10 == 1 and n % 100 != 11:
        return "голос"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "голоси"
    return "голосів"


def votes(n):
    return f"{n} {votes_word(n)}"


def project_line(p, title_len=TITLE_LEN):
    return f"№{p['id']} {html.escape(short(p['title'], title_len))}"


def gap_lines(projects, target):
    """Хто прямо попереду і позаду цільового проєкту та на скільки."""
    above = [p for p in projects if p["votes"] > target["votes"]]
    below = [p for p in projects if p["votes"] < target["votes"]]
    lines = []
    if above:
        p = above[-1]
        lines.append(f"⬆️ {p['rank']} місце (№{p['id']}) попереду на {votes(p['votes'] - target['votes'])}")
    if below:
        p = below[0]
        lines.append(f"⬇️ {p['rank']} місце (№{p['id']}) позаду на {votes(target['votes'] - p['votes'])}")
    if target["rank"] > TOP_N:
        p = projects[TOP_N - 1]
        lines.append(f"🎯 До ТОП-{TOP_N} (№{p['id']}) бракує {votes(p['votes'] - target['votes'])}")
    return lines


def change_lines(projects, target, old_rank, old_above, new_above):
    """Заголовок позачергового поста: як змінилось місце і хто кого обігнав."""
    by_id = {p["id"]: p for p in projects}
    rank = target["rank"]
    if rank < old_rank:
        head = f"🎉 <b>№{TARGET_ID} піднявся: {old_rank} → {rank} місце</b>"
    elif rank > old_rank:
        head = f"⚠️ <b>№{TARGET_ID} опустився: {old_rank} → {rank} місце</b>"
    else:
        head = f"🔄 <b>Зміни навколо №{TARGET_ID} ({rank} місце)</b>"

    lines = [head]
    for pid in sorted(old_above - new_above):
        p = by_id[pid]
        verb = "Зрівнявся з" if p["votes"] == target["votes"] else "Обігнав"
        lines.append(f"✅ {verb} {project_line(p)} ({p['votes']})")
    for pid in sorted(new_above - old_above):
        p = by_id[pid]
        lines.append(f"❌ Його обігнав {project_line(p)} ({p['votes']})")
    return lines


def post_text(projects, target, now, change=None):
    # Якщо №53 опуститься низько, довгий список може не влізти в ліміт Telegram — тоді скорочуємо назви
    for title_len in (TITLE_LEN, 25, 15):
        text = build_post(projects, target, now, change, title_len)
        if len(text) <= TELEGRAM_LIMIT:
            break
    return text


def build_post(projects, target, now, change, title_len):
    lines = [*change, "", "━━━━━━━━━━━━━━━", ""] if change else []
    lines += [f"🏆 <b>Рейтинг до №{target['id']}</b>", ""]
    # Усі місця від першого до цільового проєкту включно (разом з тими, хто з ним на одному місці)
    for p in projects:
        if p["rank"] > target["rank"]:
            break
        mark = "👉 " if p is target else ""
        lines.append(f"{p['rank']}. {mark}<b>{p['votes']}</b> · {project_line(p, title_len)}")
    lines += [
        "",
        f'📍 <b><a href="{project_url(target["id"])}">Проєкт №{target["id"]}</a>: {target["rank"]} місце</b> з {len(projects)}',
        f"🔥 {votes(target['votes'])}",
        *gap_lines(projects, target),
        "",
        f"🕒 {now:%d.%m %H:%M} (Київ)",
    ]
    return "\n".join(lines)


# --- Стан між запусками ---

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    if os.environ.get("GITHUB_ACTIONS") and not (TOKEN and CHAT_ID):
        sys.exit("::error::Не задано секрети TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID")

    projects, failed = scrape()
    target = next((p for p in projects if p["id"] == TARGET_ID), None)
    if failed or not target:
        # Неповні дані дадуть хибне місце — краще пропустити цей запуск
        print(f"::warning::Не завантажилися проєкти {sorted(failed)}, оновлення пропущено")
        return

    state = load_state()
    state.pop("message_id", None)  # залишок від версії із закріпленим повідомленням
    now = datetime.now(KYIV)
    above = sorted(p["id"] for p in projects if p["votes"] > target["votes"])

    change = None
    if "above" in state and set(state["above"]) != set(above):
        change = change_lines(projects, target, state["rank"], set(state["above"]), set(above))
    last_post = state.get("last_post")
    due = not last_post or now - datetime.fromisoformat(last_post) >= POST_EVERY - SCHEDULE_SLACK

    if not (change or due):
        print(f"Місце №{TARGET_ID} не змінилось ({target['rank']}), до планового поста ще не час")
        return

    text = post_text(projects, target, now, change)
    if not (TOKEN and CHAT_ID):
        print("Тестовий режим: немає TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID\n")
        print(text)
        return

    # Плановий пост — без звуку, зміна місця — зі звуком
    tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML",
       link_preview_options=NO_PREVIEW, disable_notification=not change)
    state["rank"] = target["rank"]
    state["above"] = above
    state["last_post"] = now.isoformat(timespec="minutes")
    save_state(state)
    print(f"Опубліковано {'пост про зміну місця' if change else 'плановий пост'}: "
          f"№{TARGET_ID} на {target['rank']} місці, {votes(target['votes'])}")


def print_chat_ids():
    global TOKEN
    TOKEN = TOKEN or getpass.getpass("Токен бота (не відображається): ").strip()
    chats = {}
    for update in tg("getUpdates"):
        for key in ("message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
    if not chats:
        print("Повідомлень немає. Напишіть боту /start і запустіть ще раз.")
    for chat_id, name in chats.items():
        print(f"{chat_id}  {name}")


if __name__ == "__main__":
    if "--chat-id" in sys.argv[1:]:
        print_chat_ids()
    else:
        main()
