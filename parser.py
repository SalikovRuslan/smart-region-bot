import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ranking import CATEGORY_NAMES, category_of, funding_rival, official_ranking, votes_to_overtake

BASE_URL = "https://smartregion.pl.ua"
# Шкільний бюджет (sb), конкурс №12 — проєкти з ID від 1 до 64
SECTION = "sb"
CONTEST_ID = 12
START_ID = 1
END_ID = 64
TARGET_ID = 53
MAX_WORKERS = 16

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Одна сесія на всі потоки: перевикористовує з'єднання і сама повторює запит при збоях
session = requests.Session()
session.headers.update(headers)
retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
adapter = HTTPAdapter(max_retries=retry, pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
session.mount("https://", adapter)


def field_value(soup, label):
    """Повертає текст блоку <p class="big"> під заголовком <h4>label</h4>."""
    h4 = soup.find("h4", string=lambda s: s and s.strip() == label)
    if not h4:
        return None
    value = h4.find_next_sibling("p")
    return value.get_text(strip=True) if value else None


def parse_project(proj_id):
    res = session.get(project_url(proj_id), timeout=10)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    title_el = soup.find("h1")
    if not title_el:
        raise ValueError("немає заголовка h1")
    title = title_el.get_text(strip=True)

    votes_text = field_value(soup, "Підтримали")
    if not votes_text:
        raise ValueError("не знайдено блок «Підтримали»")
    # Прибираємо пробіли-розділювачі тисяч, наприклад "1 793"
    votes = int(re.sub(r"\D", "", votes_text))

    return {"id": proj_id, "title": title, "votes": votes}


def project_url(proj_id):
    return f"{BASE_URL}/{SECTION}/projects/{CONTEST_ID}/{proj_id}"


def scrape(verbose=False):
    """Парсить усі проєкти. Повертає (проєкти за спаданням голосів, {id: помилка})."""
    all_projects = []
    failed = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(parse_project, pid): pid for pid in range(START_ID, END_ID + 1)}
        for future in as_completed(futures):
            pid = futures[future]
            try:
                result = future.result()
            except Exception as e:
                failed[pid] = f"{type(e).__name__}: {e}"
                if verbose:
                    print(f"⚠️  ID {pid}: {failed[pid]}")
                continue
            all_projects.append(result)
            if verbose:
                print(f"⚡ ID {result['id']:<3d} {result['title'][:40]:<40} — 🔥 {result['votes']}")

    return sorted(all_projects, key=lambda x: (-x["votes"], x["id"])), failed


def main():
    print(f"🚀 Парсинг проєктів {SECTION}/{CONTEST_ID}: ID від {START_ID} до {END_ID} у {MAX_WORKERS} потоків...")
    print("=" * 75)
    started = time.perf_counter()

    projects, failed = scrape(verbose=True)

    elapsed = time.perf_counter() - started
    print("=" * 75)
    print(f"✅ Отримано {len(projects)} з {END_ID - START_ID + 1} проєктів за {elapsed:.1f} с")
    if failed:
        print(f"❌ Не вдалося завантажити: {sorted(failed)} — рейтинг неповний!")

    category = category_of(TARGET_ID)
    if not category:
        print(f"❌ Проєкту {TARGET_ID} немає в contest.json")
        return
    rows, budget = official_ranking(projects, category)
    target = next((r for r in rows if r["id"] == TARGET_ID), None)

    # --- ОФІЦІЙНИЙ РЕЙТИНГ КАТЕГОРІЇ ДО ЦІЛЬОВОГО ПРОЄКТУ ---
    print(f"\n🏆 {CATEGORY_NAMES[category].upper()}: РЕЙТИНГ ДО ПРОЄКТУ №{TARGET_ID} (бал = голоси × √коефіцієнт)")
    print("=" * 75)
    for r in rows[: target["rank"] if target else 10]:
        short_title = r["title"] if len(r["title"]) <= 40 else r["title"][:37] + "..."
        mark = "👉" if r is target else "  "
        icon = {"full": "✅", "remainder": "🟡"}.get(r["funding"], "")
        print(f"{mark}{r['rank']:2d}. [ID {r['id']:<3d}] {short_title:<40} {r['score']:8.0f} балів ({r['votes']} гол.) {icon}")
    print("=" * 75)
    print(f"✅ повне фінансування, 🟡 залишок — оцінка, бюджет категорії ≈ {budget:.0f} тис. грн")

    # --- ВИВЕДЕННЯ РЕЗУЛЬТАТУ ЦІЛЬОВОГО ПРОЄКТУ ---
    print(f"\n📍 ПРОЄКТ №{TARGET_ID}:")
    print("-" * 75)
    if target:
        print(f"🏅 Місце: {target['rank']} з {len(rows)} ({CATEGORY_NAMES[category].lower()})")
        print(f"📋 {target['title']}")
        print(f"🔥 Голосів: {target['votes']} × √{target['coef']:.2f} = {target['score']:.0f} балів")
        if target["rank"] > 1:
            above = rows[target["rank"] - 2]
            print(f"⬆️  Обігнати {above['rank']} місце (ID {above['id']}): треба ще голосів — {votes_to_overtake(target, above)}")
        rival = funding_rival(rows, budget, target)
        if rival:
            print(f"💰 До повного фінансування: обігнати ID {rival['id']} ({rival['rank']} місце), "
                  f"треба ще голосів — {votes_to_overtake(target, rival)}")
        else:
            print("💰 Зараз у зоні повного фінансування")
    else:
        print(f"❌ Проєкт {TARGET_ID} не завантажився: {failed.get(TARGET_ID, 'невідома причина')}")
    print("-" * 75)


if __name__ == "__main__":
    main()
