"""Офіційний рейтинг шкільного громадського бюджету.

Проєкти змагаються окремо в трьох категоріях — міські, селищні та сільські громади (п. 7.6 Положення).
Остаточний бал = голоси × √ваговий коефіцієнт громади (додаток 3 до Положення).
Переможці — проєкти з верху рейтингу своєї категорії, доки вистачає коштів (п. 7.7).
"""
import json
import math
from pathlib import Path

CONTEST = json.loads(Path(__file__).with_name("contest.json").read_text(encoding="utf-8"))
# Тис. грн з обласного бюджету на ШГБ 2026 (обласна програма «Громадські бюджети Полтавщини», п. 2.5)
TOTAL_BUDGET = 6000.0
CATEGORY_NAMES = {"міські": "Міські громади", "селищні": "Селищні громади", "сільські": "Сільські громади"}


def category_of(project_id):
    info = CONTEST["projects"].get(str(project_id))
    return info["category"] if info else None


def official_ranking(projects, category):
    """Проєкти категорії за остаточним балом. Повертає (рядки з місцями, бюджет категорії).

    Як бюджет ділять між категоріями, у Положенні не прописано. У 2025 році його поділили
    пропорційно кількості проєктів у категорії — так рахуємо й тут, тому це оцінка.
    Перший проєкт, на який не вистачає повної суми, отримує залишок (так було у 2025 році).
    """
    data = CONTEST["projects"]
    rows = []
    for p in projects:
        info = data.get(str(p["id"]))
        if info and info["category"] == category:
            rows.append({**p, "coef": info["coef"], "amount": info["amount"],
                         "score": p["votes"] * math.sqrt(info["coef"])})
    # Однаковий бал — вище той, хто просить менше коштів (п. 7.8)
    rows.sort(key=lambda r: (-r["score"], r["amount"]))

    in_category = sum(1 for info in data.values() if info["category"] == category)
    budget = TOTAL_BUDGET * in_category / len(data)
    left = budget
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        if left >= r["amount"]:
            r["funding"], left = "full", left - r["amount"]
        elif left > 0:
            r["funding"], left = "remainder", 0
        else:
            r["funding"] = None
    return rows, budget


def votes_to_overtake(chaser, leader):
    """Скільки ще голосів потрібно chaser, щоб його бал став більшим за бал leader."""
    return math.floor(leader["score"] / math.sqrt(chaser["coef"])) + 1 - chaser["votes"]


def funding_rival(rows, budget, target):
    """Кого target має обігнати, щоб отримати повне фінансування (None — вже отримує)."""
    if target["funding"] == "full":
        return None
    spent = 0
    for r in rows:
        if r is target:
            continue
        if spent + r["amount"] + target["amount"] > budget:
            return r
        spent += r["amount"]
    return None
