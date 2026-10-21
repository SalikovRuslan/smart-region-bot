"""Telegram-бот: раз на годину публікує офіційний рейтинг категорії проєкту №53
(бал = голоси × √коефіцієнт громади) від 1 місця до №53 — без звуку,
а коли змінюється його місце — одразу позачерговий пост зі звуком.
Старі пости не редагуються, тож у каналі зберігається вся історія.

Запуск:          TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python bot.py
Тестовий режим:  python bot.py              (без токена — лише друкує повідомлення)
Дізнатися chat_id: python bot.py --chat-id
"""
import getpass
import html
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from parser import TARGET_ID, project_url, scrape
from ranking import CATEGORY_NAMES, category_of, funding_rival, official_ranking, votes_to_overtake

STATE_FILE = Path(__file__).with_name("state.json")
# Версія формату state.json: зі зміною логіки рейтингу старий стан не порівнюємо
STATE_VERSION = 2
TITLE_LEN = 40
KYIV = ZoneInfo("Europe/Kyiv")
NO_PREVIEW = {"is_disabled": True}
TELEGRAM_LIMIT = 4096  # максимальна довжина повідомлення
POST_EVERY = timedelta(hours=1)
# GitHub запускає розклад неточно, тому пост виходить, якщо минуло хоча б 50 хв
SCHEDULE_SLACK = timedelta(minutes=10)
FUNDING_ICONS = {"full": " ✅", "remainder": " 🟡", None: ""}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# true — публікувати пост, навіть якщо година ще не минула (ручний запуск)
FORCE_POST = os.environ.get("FORCE_POST", "").strip().lower() == "true"


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


def points(score):
    return f"{score:,.0f}".replace(",", " ")


def project_line(p, title_len=TITLE_LEN):
    return f"№{p['id']} {html.escape(short(p['title'], title_len))}"


def gap_lines(rows, budget, target):
    """Хто прямо попереду й позаду та скільки голосів бракує до межі фінансування."""
    i = rows.index(target)
    lines = []
    if i > 0:
        p = rows[i - 1]
        lines.append(f"⬆️ Обігнати {p['rank']} місце (№{p['id']}): треба ще {votes(votes_to_overtake(target, p))}")
    if i + 1 < len(rows):
        p = rows[i + 1]
        lines.append(f"⬇️ {p['rank']} місце (№{p['id']}) обжене, якщо набере ще {votes(votes_to_overtake(p, target))}")
    rival = funding_rival(rows, budget, target)
    if rival is None:
        lines.append("💰 Зараз у зоні повного фінансування ✅")
    else:
        prefix = "Зараз отримує лише залишок коштів 🟡. " if target["funding"] == "remainder" else ""
        lines.append(f"💰 {prefix}До повного фінансування: обігнати №{rival['id']} ({rival['rank']} місце), "
                     f"треба ще {votes(votes_to_overtake(target, rival))}")
    return lines


def change_lines(rows, target, old_rank, old_above, new_above):
    """Заголовок позачергового поста: як змінилось місце і хто кого обігнав."""
    by_id = {r["id"]: r for r in rows}
    rank = target["rank"]
    if rank < old_rank:
        head = f"🎉 <b>№{TARGET_ID} піднявся: {old_rank} → {rank} місце</b>"
    elif rank > old_rank:
        head = f"⚠️ <b>№{TARGET_ID} опустився: {old_rank} → {rank} місце</b>"
    else:
        head = f"🔄 <b>Зміни навколо №{TARGET_ID} ({rank} місце)</b>"

    lines = [head]
    for pid in sorted(old_above - new_above):
        r = by_id[pid]
        lines.append(f"✅ Обігнав {project_line(r)} ({points(r['score'])} балів)")
    for pid in sorted(new_above - old_above):
        r = by_id[pid]
        lines.append(f"❌ Його обігнав {project_line(r)} ({points(r['score'])} балів)")
    return lines


def post_text(rows, budget, target, now, change=None):
    # Якщо №53 опуститься низько, довгий список може не влізти в ліміт Telegram — тоді скорочуємо назви
    for title_len in (TITLE_LEN, 25, 15):
        text = build_post(rows, budget, target, now, change, title_len)
        if len(text) <= TELEGRAM_LIMIT:
            break
    return text


def build_post(rows, budget, target, now, change, title_len):
    category = CATEGORY_NAMES[category_of(target["id"])]
    lines = [*change, "", "━━━━━━━━━━━━━━━", ""] if change else []
    lines += [f"🏆 <b>{category}: рейтинг до №{target['id']}</b>",
              "<i>бал = голоси × √коефіцієнт громади</i>", ""]
    # Усі місця від першого до цільового проєкту включно
    for r in rows[: target["rank"]]:
        mark = "👉 " if r is target else ""
        lines.append(f"{r['rank']}. {mark}<b>{points(r['score'])}</b> · {project_line(r, title_len)}"
                     f" · {r['votes']} гол.{FUNDING_ICONS[r['funding']]}")
        nxt = rows[r["rank"]] if r["rank"] < target["rank"] else None
        if r["funding"] and nxt and not nxt["funding"]:
            lines.append("┄┄┄ межа фінансування ┄┄┄")
    lines += [
        "",
        f'📍 <b><a href="{project_url(target["id"])}">Проєкт №{target["id"]}</a>: {target["rank"]} місце</b>'
        f" з {len(rows)}",
        f"🔥 {votes(target['votes'])} × {math.sqrt(target['coef']):.2f}".replace(".", ",")
        + f" = {points(target['score'])} балів",
        *gap_lines(rows, budget, target),
        "",
        "<i>✅ повне фінансування, 🟡 залишок коштів — оцінка за схемою розподілу 2025 року</i>",
        f"🕒 {now:%d.%m %H:%M} (Київ)",
    ]
    return "\n".join(lines)


# --- Стан між запусками ---

def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return state if state.get("version") == STATE_VERSION else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    if os.environ.get("GITHUB_ACTIONS") and not (TOKEN and CHAT_ID):
        sys.exit("::error::Не задано секрети TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID")
    category = category_of(TARGET_ID)
    if not category:
        sys.exit(f"::error::Проєкту №{TARGET_ID} немає в contest.json")

    projects, failed = scrape()
    rows, budget = official_ranking(projects, category)
    target = next((r for r in rows if r["id"] == TARGET_ID), None)
    if failed or not target:
        # Неповні дані дадуть хибне місце — краще пропустити цей запуск
        print(f"::warning::Не завантажилися проєкти {sorted(failed)}, оновлення пропущено")
        return

    state = load_state()
    now = datetime.now(KYIV)
    above = sorted(r["id"] for r in rows[: target["rank"] - 1])

    change = None
    if "above" in state and set(state["above"]) != set(above):
        change = change_lines(rows, target, state["rank"], set(state["above"]), set(above))
    last_post = state.get("last_post")
    due = FORCE_POST or not last_post or now - datetime.fromisoformat(last_post) >= POST_EVERY - SCHEDULE_SLACK

    if not (change or due):
        print(f"Місце №{TARGET_ID} не змінилось ({target['rank']}), до планового поста ще не час")
        return

    text = post_text(rows, budget, target, now, change)
    if not (TOKEN and CHAT_ID):
        print("Тестовий режим: немає TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID\n")
        print(text)
        return

    # Плановий пост — без звуку, зміна місця — зі звуком
    tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML",
       link_preview_options=NO_PREVIEW, disable_notification=not change)
    state.update(version=STATE_VERSION, rank=target["rank"], above=above,
                 last_post=now.isoformat(timespec="minutes"))
    save_state(state)
    print(f"Опубліковано {'пост про зміну місця' if change else 'плановий пост'}: "
          f"№{TARGET_ID} на {target['rank']} місці, {points(target['score'])} балів")


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
