#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dzen_pipeline.py — автоматизация всего процесса для Яндекс Дзен (ниша «Семейные драмы»).

Этапы:  структура → таблицы ДО → статья (≥20 мин) → верификация → исправление (цикл)
        → 5 заголовков → 5 вариантов обложки.

Установка:   pip install anthropic
Ключ:        export ANTHROPIC_API_KEY="sk-ant-..."      (Windows: set ANTHROPIC_API_KEY=...)
Запуск:      python dzen_pipeline.py --topic "Свекровь пришла без звонка" --dir ./files
Пакетно:     python dzen_pipeline.py --topics-file topics.txt --dir ./files
Проверка без API (тест программы):  python dzen_pipeline.py --topic "Тест" --dir ./files --dry-run
Проверить готовый текст:            python dzen_pipeline.py --check-only статья.txt --dir ./files

В папке --dir должны лежать: ИНСТРУКЦИЯ.md, СТИЛЬ.md, СТРУКТУРА.md, ИСТОЧНИКИ.md
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

FILES = ["ИНСТРУКЦИЯ.md", "СТИЛЬ.md", "СТРУКТУРА.md", "ИСТОЧНИКИ.md"]
DEFAULT_MODEL = "claude-sonnet-5-5"
WPM = 200                 # слов в минуту (скорость чтения) — при желании поменяйте
MIN_MINUTES = 20          # требование задания: не менее 20 минут
MIN_WORDS = WPM * MIN_MINUTES
HOTLINE = "8-800-2000-122"
# Доля предложений с Но/А/И: профиль «корпус» (измерено 11% на реальных рассказах) и «строгий» (цель 33%)
STYLE_RANGE = (0.10, 0.40)
STRICT_RANGE = (0.25, 0.40)

# ───────────────────────── служебное ─────────────────────────

def read_text(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_json(text: str):
    text = re.sub(r"```(?:json)?", "", text)
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("Не удалось разобрать JSON из ответа модели:\n" + text[:500])


def words(text: str) -> list:
    return re.findall(r"[A-Za-zА-Яа-яЁё0-9-]+", text)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ───────────────────────── обёртка над API ─────────────────────────

class LLM:
    def __init__(self, model: str, system_text: str, dry_run: bool = False):
        self.model, self.system, self.dry = model, system_text, dry_run
        if not dry_run:
            import anthropic  # noqa
            self.client = anthropic.Anthropic()

    def ask(self, task: str, user: str, max_tokens: int = 8000) -> str:
        if self.dry:
            return DRY[task]
        last = None
        for attempt in range(4):
            try:
                with self.client.messages.stream(
                    model=self.model,
                    max_tokens=max_tokens,
                    # все 4 файла читаются ЦЕЛИКОМ и кэшируются — дешевле при повторных вызовах
                    system=[{"type": "text", "text": self.system,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user}],
                ) as s:
                    msg = s.get_final_message()
                return "".join(b.text for b in msg.content if b.type == "text")
            except Exception as e:  # сеть, лимиты и т.п.
                last = e
                wait = 5 * (attempt + 1)
                log(f"  API ошибка ({e}); повтор через {wait} c")
                time.sleep(wait)
        raise RuntimeError(f"API не ответил: {last}")

# ───────────────────────── ШАГ 2: структура ─────────────────────────

def step_structure(llm: LLM, topic: str) -> dict:
    p = f"""ШАГ 2. Выбери структуру из СТРУКТУРА.md (А, Б, В или Г) для темы: «{topic}».
Верни ТОЛЬКО JSON: {{"structure": "А|Б|В|Г", "name": "название", "reason": "почему, 1-2 предложения",
"story_type": "вымышленная|из жизни"}}"""
    data = extract_json(llm.ask("structure", p, 800))
    if data.get("structure") not in list("АБВГ"):
        data["structure"] = "А"
    return data

# ───────────────────────── ШАГ 3: таблицы ДО ─────────────────────────

def step_tables(llm: LLM, topic: str, st: dict) -> dict:
    p = f"""ШАГ 3. Создай таблицы ДО написания статьи. Тема: «{topic}». Структура: {st['structure']} — {st.get('name','')}.
Правила: имена/возрасты/даты — только здесь, потом копируются. Фоновые факты — ТОЛЬКО из ИСТОЧНИКИ.md;
если нужного факта там нет — не включай его (принцип «Я не знаю»). Актуальный год — 2026.
Верни ТОЛЬКО JSON:
{{
 "characters": [{{"name":"","age":0,"role":"","trait":"","speech_marker":""}}],   // 2-5 персонажей
 "chronology": [{{"time":"10 лет назад","event":"","who":"","effect":""}}],       // обязательно включи «сейчас»
 "facts": [{{"fact":"","value":"","source":"","level":"Ур.1","checked":true}}]    // может быть пустым
}}
Метки времени пиши цифрами: «2 года назад», «6 месяцев назад»."""
    t = extract_json(llm.ask("tables", p, 3000))
    for k in ("characters", "chronology", "facts"):
        t.setdefault(k, [])
    return t


def tables_md(t: dict) -> str:
    def row(*c): return "| " + " | ".join(str(x).replace("|", "/") for x in c) + " |"
    out = ["### 2.1 Персонажи", "| Имя | Возраст | Роль | Ключевая черта | Речевой маркер |", "|---|---|---|---|---|"]
    out += [row(c.get("name"), c.get("age"), c.get("role"), c.get("trait"), c.get("speech_marker")) for c in t["characters"]]
    out += ["", "### 2.2 Хронология", "| Метка | Событие | Кто | Эффект |", "|---|---|---|---|"]
    out += [row(c.get("time"), c.get("event"), c.get("who"), c.get("effect")) for c in t["chronology"]]
    out += ["", "### 2.3 Фоновые факты", "| Факт | Значение | Источник | Уровень | Проверено |", "|---|---|---|---|---|"]
    out += [row(c.get("fact"), c.get("value"), c.get("source"), c.get("level"), "✅" if c.get("checked") else "❌") for c in t["facts"]]
    if not t["facts"]:
        out.append("| *(фоновых фактов нет — статья без статистики)* | | | | |")
    return "\n".join(out)

# ───────────────────────── ШАГ 4: статья ─────────────────────────

def step_write(llm: LLM, topic: str, st: dict, t: dict) -> str:
    p = f"""ШАГ 4. Напиши статью на тему «{topic}» для Яндекс Дзен, ниша «Семейные драмы».
Структура: {st['structure']} — {st.get('name','')} (блоки 1–4 из СТРУКТУРА.md). Тип истории: {st.get('story_type','вымышленная')}.
Объём: НЕ МЕНЕЕ {MIN_WORDS} слов (≥{MIN_MINUTES} минут чтения). Это жёсткий минимум — пиши развёрнуто:
больше сцен, диалогов, бытовых деталей, внутреннего монолога; НЕ растягивай клише и повторами.

ТАБЛИЦЫ (копируй имена, возрасты, метки времени и числа отсюда, НЕ изобретай заново):
{tables_md(t)}

Требования: СТИЛЬ.md (тон, ритм, ~33% предложений с Но/А/И, кульминация — самое короткое предложение),
запрещённые клише 2.4, финал без морали, монетизация Дзена (событие → реакция, романтика без физиологии),
источники только из ИСТОЧНИКИ.md, нет факта — метка [ТРЕБУЕТ ИСТОЧНИКА: "..."]. Год — 2026.
Если «из жизни» — в конце: «Имена изменены».
Верни ТОЛЬКО текст статьи (первая строка — абзац истории, без заголовков-пояснений и без комментариев)."""
    art = llm.ask("write", p, 32000).strip()
    for i in range(3):
        n = len(words(art))
        if n >= MIN_WORDS:
            break
        log(f"  Короткая статья: {n} слов < {MIN_WORDS}. Расширяю (попытка {i + 1}/3)")
        p2 = f"""Статья слишком короткая: {n} слов, нужно не менее {MIN_WORDS}.
Расширь её: добавь новые сцены и диалоги в блоке «Нарастание», флэшбэки (только из таблицы 2.2), бытовые детали.
Не меняй имена, возрасты и хронологию. Финал оставь без морали.
Верни ПОЛНЫЙ текст статьи целиком, без комментариев.

ТЕКУЩИЙ ТЕКСТ:
{art}"""
        art = llm.ask("write", p2, 32000).strip()
    return art

# ───────────────────────── ШАГ 5: верификация ─────────────────────────

NUM_WORDS = {"один": 1, "одного": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
             "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
             "пятнадцать": 15, "двадцать": 20}
BAD_THEMES = ["изнасил", "труп", "кровь", "кровью", "кровав", "самоубий", "суицид", "расчленен", "пытк", "порно"]
VIOLENCE = ["избил", "избива", "ударил", "бил ", "насили", "побои", "душил"]
MORAL = ["берегите", "ценит", "помните", "главное в жизни", "семья — это", "не забывайте", "будьте счастливы"]
EXTRA_CLICHES = ["сердце сжалось", "слёзы ручьём", "слезы ручьем", "мир рухнул", "душа разрывалась",
                 "в данной ситуации", "на протяжении длительного времени", "взаимоотношения между супругами"]


def sentences(text: str) -> list:
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text)
    return [re.sub(r"^[—–\-\s«»\"]+", "", s).strip() for s in parts if s.strip()]


def norm_marker(m: str) -> str:
    m = m.lower().strip()
    toks = m.split()
    if toks and toks[0] in NUM_WORDS:
        toks[0] = str(NUM_WORDS[toks[0]])
    m = " ".join(toks)
    m = re.sub(r"\bгода\b|\bгод\b", "лет", m)
    m = re.sub(r"месяц\w*", "месяцев", m)
    m = re.sub(r"недел\w*", "недель", m)
    return m


def cliches_from_structure(structure_md: str) -> list:
    sec = structure_md.split("2.4", 1)[-1].split("## 3", 1)[0]
    found = re.findall(r"^\|\s*«([^»]+)»\s*\|", sec, flags=re.M)
    return [f.strip(" .…").lower() for f in found] + EXTRA_CLICHES


def repeat_targets(structure_md: str) -> list:
    sec = structure_md.split("3.5", 1)[-1]
    found = re.findall(r"^\|\s*«([^»]+)»\s*\|", sec, flags=re.M)
    return [f.lower() for f in found] or ["предательство", "больно", "она поняла"]


def local_checks(article: str, t: dict, structure_md: str, st: dict) -> list:
    """Возвращает строки: (таблица, пункт, статус ✅/❌/⚠️, детали)."""
    R = []
    low = article.lower()
    n = len(words(article))

    # объём
    R.append(("3.3 Чеклист", f"Объём ≥ {MIN_MINUTES} мин ({MIN_WORDS} слов)",
              "✅" if n >= MIN_WORDS else "❌", f"{n} слов ≈ {n / WPM:.1f} мин"))

    # клише 2.4
    hits = [c for c in cliches_from_structure(structure_md) if c in low]
    R.append(("2.4 Клише", "Запрещённые клише", "✅" if not hits else "❌", "; ".join(hits) or "не найдены"))

    # повторы 3.5
    bad = []
    for ph in repeat_targets(structure_md):
        stem = ph if " " in ph else ph[:max(5, len(ph) - 3)]
        c = len(re.findall(re.escape(stem), low))
        R.append(("3.5 Повторы", f"«{ph}»", "✅" if c <= 2 else "❌", f"{c} раз(а)"))
    ws = [w.lower() for w in words(article)]
    grams = Counter(" ".join(ws[i:i + 4]) for i in range(len(ws) - 3))
    for g, c in grams.most_common(40):
        if c > 2:
            bad.append(f"«{g}» ×{c}")
    R.append(("3.5 Повторы", "Одинаковые 4-словные обороты >2 раз", "✅" if not bad else "❌", "; ".join(bad[:8]) or "нет"))

    # Но / А / И
    ss = sentences(article)
    share = sum(1 for s in ss if s.split(" ")[0].lower() in ("но", "а", "и")) / max(1, len(ss))
    lo, hi = STYLE_RANGE
    R.append(("3.3 Чеклист", f"Предложений с Но/А/И: допуск {lo:.0%}–{hi:.0%}",
              "✅" if lo <= share <= hi else "❌", f"{share:.0%} из {len(ss)} предложений"))

    # финал без морали
    last = " ".join(article.strip().split("\n")[-3:]).lower()
    m = [x for x in MORAL if re.search(r"\b" + re.escape(x), last)]
    R.append(("3.3 Чеклист", "Финал без морали", "✅" if not m else "❌", ", ".join(m) or "ок"))

    # монетизация
    bt = [x.strip() for x in BAD_THEMES if re.search(r"\b" + re.escape(x.strip()), low)]
    R.append(("3.3 Чеклист", "Нет деталей травм/смерти/эротики", "✅" if not bt else "❌", ", ".join(bt) or "ок"))
    viol = [x.strip() for x in VIOLENCE if re.search(r"\b" + re.escape(x.strip()), low)]
    if viol:
        ok = HOTLINE in article and "предупрежд" in low
        R.append(("3.3 Чеклист", f"Насилие: триггер-предупреждение + {HOTLINE}", "✅" if ok else "❌", ", ".join(viol)))
    else:
        R.append(("3.3 Чеклист", "Насилие: триггер-предупреждение + телефон", "✅", "н/а"))

    # анонимизация
    if st.get("story_type") == "из жизни":
        R.append(("3.3 Чеклист", "«Имена изменены»", "✅" if "имена изменены" in low else "❌", ""))

    # метки «Я не знаю»
    miss = re.findall(r"\[ТРЕБУЕТ ИСТОЧНИКА[^\]]*\]", article)
    R.append(("3.1 Факты", "Не осталось меток [ТРЕБУЕТ ИСТОЧНИКА]", "✅" if not miss else "❌", "; ".join(miss) or "нет"))

    # хронология 3.4
    table_marks = {norm_marker(c.get("time", "")) for c in t["chronology"]}
    marks = re.findall(r"\b((?:\d+|[а-яё]+)\s+(?:лет|года|год|месяц\w*|недел\w*)\s+назад)", low)
    for mk in sorted(set(marks)):
        nm = norm_marker(mk)
        if nm.split()[0].isdigit() or nm.split()[0] in NUM_WORDS.values():
            R.append(("3.4 Хронология", f"«{mk}»", "✅" if nm in table_marks else "❌",
                      "есть в 2.2" if nm in table_marks else "нет в таблице 2.2"))
    if not marks:
        R.append(("3.4 Хронология", "Временные маркеры «N … назад»", "⚠️", "в тексте не найдены"))

    # имена / персонажи 2.1
    names = {c["name"].split()[0] for c in t["characters"] if c.get("name")}
    mid_caps = Counter(re.findall(r"(?<=[а-яё,]\s)([А-ЯЁ][а-яё]{2,})", article))
    unknown = [w for w, c in mid_caps.items() if c >= 3 and not any(w.startswith(nm[:max(3, len(nm) - 2)]) for nm in names)]
    R.append(("2.1 Персонажи", "Нет «лишних» имён вне таблицы", "✅" if not unknown else "⚠️",
              ", ".join(unknown[:8]) or "ок"))
    for nm in names:
        c = len(re.findall(re.escape(nm[:max(3, len(nm) - 2)]), article))
        R.append(("2.1 Персонажи", f"{nm} встречается в тексте", "✅" if c else "❌", f"{c} раз(а)"))

    # числа/статистика ↔ таблица 2.3
    facts_blob = json.dumps(t["facts"], ensure_ascii=False)
    nums = re.findall(r"\d[\d\s.,]*\s?(?:%|процент\w*|тыс\w*|млн\w*)", article)
    for x in sorted(set(nums)):
        digits = re.sub(r"[^\d]", "", x)
        ok = digits and digits in re.sub(r"[^\d]", "", facts_blob)
        R.append(("3.1 Факты", f"Число «{x.strip()}»", "✅" if ok else "❌", "в 2.3" if ok else "нет в таблице 2.3"))

    # год
    old = sorted(set(re.findall(r"\b(20(?:1\d|2[0-5]))\b", article)))
    R.append(("3.3 Чеклист", "Текущий год — только 2026", "✅" if not old else "⚠️",
              ("упомянуты: " + ", ".join(old)) if old else "ок"))
    return R


def llm_review(llm: LLM, article: str, t: dict) -> dict:
    p = f"""ШАГ 5. Верификация через таблицы СТРУКТУРА.md. Проверь статью по пунктам, которые код проверить не может.
Верни ТОЛЬКО JSON:
{{
 "facts":  [{{"fact":"","source":"","level":"Ур.1","status":"✅|❌"}}],         // таблица 3.1: все факты/законы/числа
 "quotes": [{{"quote":"","who":"","qualification":"","source":"","status":"✅|❌"}}], // таблица 3.2: все цитаты экспертов
 "checklist": {{"tone_shift": false, "romance_ok": true, "medical_ok": true, "level4_sources_used": false}},
 "errors": ["конкретная ошибка с цитатой из текста"]
}}
Нет факта/цитаты в ИСТОЧНИКИ.md → status ❌. Таблица фактов 2.3 для сверки:
{json.dumps(t['facts'], ensure_ascii=False)}

СТАТЬЯ:
{article}"""
    d = extract_json(llm.ask("review", p, 6000))
    for k, v in (("facts", []), ("quotes", []), ("checklist", {}), ("errors", [])):
        d.setdefault(k, v)
    return d


def verify(llm: LLM, article: str, t: dict, structure_md: str, st: dict):
    rows = local_checks(article, t, structure_md, st)
    rev = llm_review(llm, article, t)
    ck = rev["checklist"]
    rows += [
        ("3.3 Чеклист", "Нет смены тона (разговорный → публицистический)", "❌" if ck.get("tone_shift") else "✅", ""),
        ("3.3 Чеклист", "Романтика: слово + одно действие", "✅" if ck.get("romance_ok", True) else "❌", ""),
        ("3.3 Чеклист", "Медицина: реакция, не процесс", "✅" if ck.get("medical_ok", True) else "❌", ""),
        ("3.3 Чеклист", "Источники уровня 4 не использованы", "❌" if ck.get("level4_sources_used") else "✅", ""),
    ]
    for f in rev["facts"]:
        rows.append(("3.1 Факты", f.get("fact", ""), f.get("status", "❌"), f"{f.get('source', '')} ({f.get('level', '')})"))
    for q in rev["quotes"]:
        rows.append(("3.2 Цитаты", q.get("quote", ""), q.get("status", "❌"), f"{q.get('who', '')}, {q.get('qualification', '')}, {q.get('source', '')}"))
    errors = [f"[{a}] {b}: {d}" for a, b, s, d in rows if s == "❌"]
    errors += [f"[LLM] {e}" for e in rev["errors"]]
    return rows, errors


def verification_md(rows: list, errors: list) -> str:
    out = []
    for tab in dict.fromkeys(r[0] for r in rows):
        out += [f"### Таблица {tab}", "| Пункт | Статус | Детали |", "|---|---|---|"]
        out += [f"| {b} | {s} | {d} |".replace("\n", " ") for a, b, s, d in rows if a == tab]
        out.append("")
    out.append("## СТАТУС")
    out.append("✅ Статья готова к публикации" if not errors else "❌ Найдены ошибки:\n" + "\n".join(f"- {e}" for e in errors))
    return "\n".join(out)

# ───────────────────────── Задание 2: исправление ─────────────────────────

def step_fix(llm: LLM, article: str, errors: list, t: dict):
    p = f"""Задание 2. В верификации найдены ошибки:
{chr(10).join('- ' + e for e in errors)}

1) Для каждой ошибки определи: какая слабость ИИ привела к ошибке и какая таблица/правило нарушены.
2) Исправь статью (ИНСТРУКЦИЯ.md, СТИЛЬ.md, СТРУКТУРА.md). Имена/возрасты/метки времени — как в таблицах:
{tables_md(t)}
Объём не должен упасть ниже {MIN_WORDS} слов. Исправляй точечно, остальной текст сохрани.

ФОРМАТ ОТВЕТА (строго):
## АНАЛИЗ ОШИБОК
...
## ИСПРАВЛЕННАЯ СТАТЬЯ
<полный текст статьи>

ТЕКУЩАЯ СТАТЬЯ:
{article}"""
    r = llm.ask("fix", p, 32000)
    parts = re.split(r"##\s*ИСПРАВЛЕННАЯ СТАТЬЯ\s*", r, maxsplit=1)
    analysis = parts[0].replace("## АНАЛИЗ ОШИБОК", "").strip()
    fixed = parts[1].strip() if len(parts) == 2 else article
    return analysis, fixed

# ───────────────────────── Задание 3: заголовки ─────────────────────────

def step_titles(llm: LLM, topic: str, article: str) -> str:
    p = f"""Задание 3. Ты — эксперт по заголовкам для Яндекс Дзен. Ниша «Семейные драмы». Тема: «{topic}».
5 вариантов: суть без кликбейта и сенсационности, соответствует тексту, вызывает любопытство; 40–60 символов, максимум 80.
Верни ТОЛЬКО JSON: {{"titles": ["..",".."], "best": 1, "why": ".."}}

КРАТКИЙ КОНТЕКСТ (начало статьи):
{article[:2500]}"""
    d = None
    for _ in range(2):
        d = extract_json(llm.ask("titles", p, 1200))
        if all(len(x) <= 80 for x in d["titles"]):
            break
        p += "\nПредыдущая попытка нарушила лимит 80 символов — сократи."
    lens = [len(x) for x in d["titles"]]
    best = int(d.get("best", 1))
    if not (1 <= best <= len(lens)) or not (40 <= lens[best - 1] <= 80):
        ok = [i for i, l in enumerate(lens, 1) if 40 <= l <= 60]
        best = ok[0] if ok else 1
    lines = [f"{i}. {x} ({len(x)} символов)" for i, x in enumerate(d["titles"], 1)]
    return "\n".join(lines) + f"\n\nРЕКОМЕНДАЦИЯ: {best}, потому что {d.get('why', '')}"

# ───────────────────────── Задание 4: обложки ─────────────────────────

def step_covers(llm: LLM, topic: str, article: str) -> str:
    p = f"""Задание 4. Ты эксперт по обложкам Яндекс Дзен. Тема: «{topic}». Раздел: Семейные драмы.
Сделай 5 быстрых вариантов обложки (А–Д) для нейросети Stable Diffusion по истории ниже.
Требования: реалистичное фото, персонажи русской национальности (славянская внешность), лица КРУПНЫМ планом,
близко к камере, сразу цепляют взгляд эмоцией; без крови, насилия и обнажёнки (монетизация); без надписей.
Для каждого варианта: описание сцены по-русски + готовый промпт на английском + negative prompt.

ИСТОРИЯ (начало):
{article[:3500]}"""
    return llm.ask("covers", p, 3500).strip()

# ───────────────────────── Запуск одной темы ─────────────────────────

def run_topic(topic: str, folder: Path, out_root: Path, model: str, dry: bool, max_fixes: int):
    docs = {f: read_text(folder / f) for f in FILES if (folder / f).exists()}
    missing = [f for f in FILES if f not in docs]
    if missing:
        sys.exit(f"Не найдены файлы в {folder}: {', '.join(missing)}")
    system = ("Ты — система генерации контента для Яндекс Дзен. Ниже ВСЕ рабочие файлы целиком — "
              "прочитай их от начала до конца и следуй им строго.\n\n" +
              "\n\n".join(f"===== {n} =====\n{docs[n]}" for n in FILES))
    llm = LLM(model, system, dry)

    slug = re.sub(r"[^\w]+", "_", topic)[:50].strip("_")
    out = out_root / f"{time.strftime('%Y%m%d_%H%M')}_{slug}"
    out.mkdir(parents=True, exist_ok=True)

    log(f"ТЕМА: {topic}")
    log("Шаг 2: выбор структуры…")
    st = step_structure(llm, topic)
    (out / "1_structure.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

    log("Шаг 3: таблицы ДО написания…")
    t = step_tables(llm, topic, st)
    (out / "2_tables.json").write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "2_tables.md").write_text(tables_md(t), encoding="utf-8")

    log("Шаг 4: пишу статью…")
    art = step_write(llm, topic, st, t)

    log("Шаг 5: верификация…")
    rows, errors = verify(llm, art, t, docs["СТРУКТУРА.md"], st)
    history = []
    for i in range(1, max_fixes + 1):
        if not errors:
            break
        log(f"  Ошибок: {len(errors)}. Исправление {i}/{max_fixes}…")
        analysis, art = step_fix(llm, art, errors, t)
        history.append(f"### Цикл исправления {i}\n**Ошибки:**\n" + "\n".join(f"- {e}" for e in errors) + f"\n\n**Анализ:**\n{analysis}\n")
        rows, errors = verify(llm, art, t, docs["СТРУКТУРА.md"], st)

    (out / "3_article.md").write_text(art, encoding="utf-8")
    (out / "4_verification.md").write_text(verification_md(rows, errors) + "\n\n" + "\n".join(history), encoding="utf-8")

    log("Задание 3: заголовки…")
    (out / "5_titles.md").write_text(step_titles(llm, topic, art), encoding="utf-8")
    log("Задание 4: обложки…")
    (out / "6_covers.md").write_text(step_covers(llm, topic, art), encoding="utf-8")

    n = len(words(art))
    status = "✅ ГОТОВО" if not errors else f"❌ осталось ошибок: {len(errors)} (см. 4_verification.md)"
    log(f"{status} | {n} слов ≈ {n / WPM:.1f} мин | папка: {out}")
    return out, not errors

# ───────────────────────── Заготовки для --dry-run ─────────────────────────

DRY = {
    "structure": '{"structure":"Б","name":"Конфликт поколений","reason":"тест","story_type":"вымышленная"}',
    "tables": json.dumps({
        "characters": [{"name": "Марина", "age": 34, "role": "героиня", "trait": "сдержанная", "speech_marker": "Ну и ладно"},
                       {"name": "Нина Петровна", "age": 62, "role": "свекровь", "trait": "контроль", "speech_marker": "Я только хочу"}],
        "chronology": [{"time": "3 года назад", "event": "свадьба", "who": "Марина", "effect": "старт"},
                       {"time": "сейчас", "event": "ультиматум", "who": "все", "effect": "финал"}],
        "facts": []}, ensure_ascii=False),
    "write": "Свекровь звонила трижды в день. Марина знала: если Нина Петровна звонит, значит, что-то не так. "
             "Но она брала трубку. И слушала. А потом, как обычно, говорила «Ну и ладно». "
             "Три года назад всё было иначе. Сердце сжалось от боли. Берегите тех, кто рядом.",
    "review": '{"facts":[],"quotes":[],"checklist":{"tone_shift":false,"romance_ok":true,"medical_ok":true,"level4_sources_used":false},"errors":[]}',
    "fix": "## АНАЛИЗ ОШИБОК\n(тест)\n## ИСПРАВЛЕННАЯ СТАТЬЯ\nСвекровь звонила трижды в день. Но Марина брала трубку. А потом молчала.",
    "titles": '{"titles":["Свекровь звонила трижды в день, а я брала трубку","Ультиматум за семейным столом","Твоя мать или я","Звонок, после которого всё изменилось","Три звонка в день: как Марина устала молчать"],"best":1,"why":"раскрывает суть без кликбейта"}',
    "covers": "Вариант А — (тест)",
}

# ───────────────────────── CLI ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Автоматизация создания статей для Яндекс Дзен")
    ap.add_argument("--topic", help="тема статьи")
    ap.add_argument("--topics-file", help="файл со списком тем (по одной в строке)")
    ap.add_argument("--dir", default=".", help="папка с ИНСТРУКЦИЯ/СТИЛЬ/СТРУКТУРА/ИСТОЧНИКИ .md")
    ap.add_argument("--out", default="./out", help="куда сохранять результат")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-fixes", type=int, default=3, help="максимум циклов исправления (Задание 2)")
    ap.add_argument("--strict-style", action="store_true", help="строгий допуск Но/А/И 25–40% вместо 10–40%")
    ap.add_argument("--dry-run", action="store_true", help="тест без обращения к API")
    ap.add_argument("--check-only", metavar="FILE", help="только проверить готовый текст локальными проверками")
    a = ap.parse_args()
    folder = Path(a.dir)
    global STYLE_RANGE
    if a.strict_style:
        STYLE_RANGE = STRICT_RANGE

    if a.check_only:
        structure_md = read_text(folder / "СТРУКТУРА.md")
        art = read_text(Path(a.check_only))
        t = {"characters": [], "chronology": [], "facts": []}
        rows = local_checks(art, t, structure_md, {"story_type": "вымышленная"})
        print(verification_md(rows, [f"[{x}] {y}: {z}" for x, y, s, z in rows if s == "❌"]))
        return

    topics = []
    if a.topic:
        topics.append(a.topic)
    if a.topics_file:
        topics += [l.strip() for l in read_text(Path(a.topics_file)).splitlines() if l.strip()]
    if not topics:
        ap.error("укажите --topic или --topics-file")
    for tp in topics:
        run_topic(tp, folder, Path(a.out), a.model, a.dry_run, a.max_fixes)


if __name__ == "__main__":
    main()
