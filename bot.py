"""Telegram-бот: оновлює закріплене повідомлення з ТОП-10 і проєктом №53
та надсилає сповіщення, коли змінюється його місце.

Запуск:          TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python bot.py
Тестовий режим:  python bot.py              (без токена — лише друкує повідомлення)
Дізнатися chat_id: python bot.py --chat-id
"""
import getpass
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from parser import TARGET_ID, project_url, scrape

STATE_FILE = Path(__file__).with_name("state.json")
TOP_N = 10
TITLE_LEN = 40
KYIV = ZoneInfo("Europe/Kyiv")
NO_PREVIEW = {"is_disabled": True}

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

def short(title):
    return title if len(title) <= TITLE_LEN else title[: TITLE_LEN - 1] + "…"


def votes_word(n):
    if n % 10 == 1 and n % 100 != 11:
        return "голос"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "голоси"
    return "голосів"


def votes(n):
    return f"{n} {votes_word(n)}"


def project_line(p):
    return f"№{p['id']} {html.escape(short(p['title']))}"


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


def pinned_text(projects, target, now):
    lines = [f"🏆 <b>ТОП-{TOP_N} шкільного бюджету</b>", ""]
    for p in projects[:TOP_N]:
        mark = "👉 " if p is target else ""
        lines.append(f"{p['rank']}. {mark}<b>{p['votes']}</b> · {project_line(p)}")
    lines += [
        "",
        f'📍 <b><a href="{project_url(target["id"])}">Проєкт №{target["id"]}</a>: {target["rank"]} місце</b> з {len(projects)}',
        f"🔥 {votes(target['votes'])}",
        *gap_lines(projects, target),
        "",
        f"🕒 Оновлено {now:%d.%m %H:%M} (Київ)",
    ]
    return "\n".join(lines)


def change_text(projects, target, old_rank, old_above, new_above):
    by_id = {p["id"]: p for p in projects}
    rank = target["rank"]
    if rank < old_rank:
        head = f"🎉 <b>№{TARGET_ID} піднявся: {old_rank} → {rank} місце</b>"
    elif rank > old_rank:
        head = f"⚠️ <b>№{TARGET_ID} опустився: {old_rank} → {rank} місце</b>"
    else:
        head = f"🔄 <b>Зміни навколо №{TARGET_ID} ({rank} місце)</b>"

    lines = [head, ""]
    for pid in sorted(old_above - new_above):
        p = by_id[pid]
        verb = "Зрівнявся з" if p["votes"] == target["votes"] else "Обігнав"
        lines.append(f"✅ {verb} {project_line(p)} ({p['votes']})")
    for pid in sorted(new_above - old_above):
        p = by_id[pid]
        lines.append(f"❌ Його обігнав {project_line(p)} ({p['votes']})")
    lines += ["", f"🔥 {votes(target['votes'])}", *gap_lines(projects, target)]
    return "\n".join(lines)


# --- Стан між запусками ---

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --- Telegram ---

def send(text):
    return tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML", link_preview_options=NO_PREVIEW)


def update_pinned(state, text):
    """Редагує закріплене повідомлення; якщо його немає — надсилає нове і закріплює."""
    if state.get("message_id"):
        try:
            tg("editMessageText", chat_id=CHAT_ID, message_id=state["message_id"], text=text,
               parse_mode="HTML", link_preview_options=NO_PREVIEW)
            return
        except TelegramError as e:
            if "message is not modified" in str(e):
                return
            if "message to edit not found" not in str(e) and "message can't be edited" not in str(e):
                raise
            print(f"Закріплене повідомлення недоступне ({e}), надсилаю нове")
    msg = send(text)
    tg("pinChatMessage", chat_id=CHAT_ID, message_id=msg["message_id"], disable_notification=True)
    state["message_id"] = msg["message_id"]


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
    above = sorted(p["id"] for p in projects if p["votes"] > target["votes"])
    text = pinned_text(projects, target, datetime.now(KYIV))
    notification = None
    if "above" in state and set(state["above"]) != set(above):
        notification = change_text(projects, target, state["rank"], set(state["above"]), set(above))

    if not (TOKEN and CHAT_ID):
        print("Тестовий режим: немає TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID\n")
        print(text)
        if notification:
            print("\n--- сповіщення ---\n" + notification)
        return

    update_pinned(state, text)
    if notification:
        send(notification)
    state["rank"] = target["rank"]
    state["above"] = above
    save_state(state)
    print(f"Готово: №{TARGET_ID} на {target['rank']} місці, {votes(target['votes'])}"
          + (", надіслано сповіщення" if notification else ""))


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
