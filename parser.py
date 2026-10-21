import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
    """Парсить усі проєкти. Повертає (рейтинг за спаданням голосів, {id: помилка})."""
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

    sorted_projects = sorted(all_projects, key=lambda x: (-x["votes"], x["id"]))

    # Однакова кількість голосів — однакове місце (1, 2, 2, 4)
    for i, proj in enumerate(sorted_projects):
        if i > 0 and proj["votes"] == sorted_projects[i - 1]["votes"]:
            proj["rank"] = sorted_projects[i - 1]["rank"]
        else:
            proj["rank"] = i + 1

    return sorted_projects, failed


def main():
    print(f"🚀 Парсинг проєктів {SECTION}/{CONTEST_ID}: ID від {START_ID} до {END_ID} у {MAX_WORKERS} потоків...")
    print("=" * 75)
    started = time.perf_counter()

    sorted_projects, failed = scrape(verbose=True)

    elapsed = time.perf_counter() - started
    print("=" * 75)
    print(f"✅ Отримано {len(sorted_projects)} з {END_ID - START_ID + 1} проєктів за {elapsed:.1f} с")
    if failed:
        print(f"❌ Не вдалося завантажити: {sorted(failed)} — рейтинг неповний!")

    target_project = next((p for p in sorted_projects if p["id"] == TARGET_ID), None)
    # Усі місця від першого до цільового проєкту (якщо він не завантажився — ТОП-10)
    last_rank = target_project["rank"] if target_project else 10

    # --- ВИВЕДЕННЯ РЕЙТИНГУ ДО ЦІЛЬОВОГО ПРОЄКТУ ---
    print(f"\n🏆 РЕЙТИНГ ДО ПРОЄКТУ №{TARGET_ID}:")
    print("=" * 75)
    for proj in sorted_projects:
        if proj["rank"] > last_rank:
            break
        short_title = proj["title"] if len(proj["title"]) <= 45 else proj["title"][:42] + "..."
        mark = "👉" if proj is target_project else "  "
        print(f"{mark}{proj['rank']:2d}. [ID {proj['id']:<3d}] {short_title:<45} — 🔥 {proj['votes']}")
    print("=" * 75)

    # --- ВИВЕДЕННЯ РЕЗУЛЬТАТУ ЦІЛЬОВОГО ПРОЄКТУ ---

    print(f"\n📍 ПРОЄКТ №{TARGET_ID}:")
    print("-" * 75)
    if target_project:
        print(f"🏅 Місце в рейтингу: {target_project['rank']} з {len(sorted_projects)}")
        print(f"📋 {target_project['title']}")
        print(f"🔥 Голосів: {target_project['votes']}")
        leader = sorted_projects[0]
        if target_project is not leader:
            print(f"📉 Відставання від лідера (ID {leader['id']}): {leader['votes'] - target_project['votes']}")
        if target_project["rank"] > 1:
            above = next(p for p in reversed(sorted_projects) if p["votes"] > target_project["votes"])
            print(f"⬆️  До попереднього місця (ID {above['id']}) не вистачає: {above['votes'] - target_project['votes']}")
    else:
        print(f"❌ Проєкт {TARGET_ID} не завантажився: {failed.get(TARGET_ID, 'невідома причина')}")
    print("-" * 75)


if __name__ == "__main__":
    main()
