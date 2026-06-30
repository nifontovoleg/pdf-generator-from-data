import os
import sys
import json
import csv
import platform
import subprocess
from pathlib import Path
from datetime import datetime
import re

try:
    import pandas as pd  # pyright: ignore[reportMissingImports]
except ImportError:
    pd = None

from weasyprint import HTML  # pyright: ignore[reportMissingImports]


# -------- Настройки директорий --------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
FONTS_DIR = BASE_DIR / "fonts"  # необязательно; можно положить сюда DejaVuSans.ttf


# -------- Вспомогательные функции --------

def list_data_files():
    files = []
    if DATA_DIR.exists():
        for p in sorted(DATA_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in [".csv", ".json"]:
                files.append(p)
    return files


def list_templates():
    files = []
    if TEMPLATES_DIR.exists():
        for p in sorted(TEMPLATES_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in [".html", ".htm"]:
                files.append(p)
    return files


def choose_from_list(title, items):
    if not items:
        print(f"\n{title}: (нет доступных вариантов)")
        return None

    print(f"\n{title}:")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")

    while True:
        choice = input("Введите номер варианта: ").strip()
        if not choice.isdigit():
            print("Введите номер (целое число).")
            continue
        idx = int(choice)
        if 1 <= idx <= len(items):
            return items[idx - 1]
        print("Неверный номер, попробуйте ещё раз.")


def load_data_file(path: Path):
    ext = path.suffix.lower()
    if ext == ".csv":
        if pd is not None:
            df = pd.read_csv(path)
            records = df.to_dict(orient="records")
        else:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                records = list(reader)
    elif ext == ".json":
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Ожидаем список записей
        if isinstance(data, dict):
            # если словарь, пробуем взять один из часто используемых ключей
            for key in ("invoices", "items", "data", "rows"):
                if key in data and isinstance(data[key], list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise ValueError("JSON-файл должен содержать список объектов (записей).")
        records = data
    else:
        raise ValueError("Неизвестный формат файла данных.")
    return records


def detect_invoice_id_key(record: dict):
    """Пытаемся угадать поле с invoice id в одной записи."""
    candidates = []
    for key in record.keys():
        key_lower = key.lower()
        if "invoice" in key_lower and "id" in key_lower:
            candidates.append(key)
        elif key_lower in ("invoice", "invoiceid", "id", "номер", "номер счета", "номер_счета"):
            candidates.append(key)
    if candidates:
        # предпочитаем самые длинные/конкретные
        candidates.sort(key=lambda x: -len(x))
        return candidates[0]
    # по умолчанию пробуем просто 'invoice_id'
    if "invoice_id" in record:
        return "invoice_id"
    return None


def extract_invoice_ids(records):
    if not records:
        return [], None
    key = detect_invoice_id_key(records[0])
    if not key:
        # пробуем по всем ключам найти 'invoice_id'
        for rec in records:
            if "invoice_id" in rec:
                key = "invoice_id"
                break
    if not key:
        raise ValueError("Не удалось определить поле invoice id в данных.")
    ids = []
    for rec in records:
        if key in rec and rec[key] not in (None, ""):
            ids.append(str(rec[key]))
    # уникальные в порядке появления
    seen = set()
    unique_ids = []
    for v in ids:
        if v not in seen:
            seen.add(v)
            unique_ids.append(v)
    return unique_ids, key


def choose_invoice_id(invoice_ids):
    return choose_from_list("Доступные чеки (invoice id)", invoice_ids)


def read_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SafeDict(dict):
    """Для безопасной подстановки в шаблон: отсутствующие ключи -> пустая строка."""
    def __missing__(self, key):
        return ""


def build_font_css():
    # Если есть локальный шрифт, используем @font-face
    local_font = None
    for name in ("DejaVuSans.ttf", "DejaVuSans-Regular.ttf", "Roboto-Regular.ttf"):
        candidate = FONTS_DIR / name
        if candidate.exists():
            local_font = candidate
            break

    if local_font:
        font_url = local_font.as_uri()
        font_face = f"""
@font-face {{
    font-family: 'CustomCyrillicFont';
    src: url('{font_url}') format('truetype');
}}
"""
        family = "CustomCyrillicFont, 'DejaVu Sans', 'Roboto', 'Arial', sans-serif"
    else:
        font_face = ""
        family = "'DejaVu Sans', 'Roboto', 'Arial', sans-serif"

    css = f"""
<style>
{font_face}
html, body {{
    font-family: {family};
}}
</style>
"""
    return css


def inject_font_css_into_html(html: str) -> str:
    css = build_font_css()
    # пытаемся вставить в <head>
    if re.search(r"(?i)<head[^>]*>", html):
        return re.sub(r"(?i)<head[^>]*>", lambda m: m.group(0) + css, html, count=1)
    elif re.search(r"(?i)<html[^>]*>", html):
        # вставляем <head> после <html>
        return re.sub(
            r"(?i)<html[^>]*>",
            lambda m: m.group(0) + "<head>" + css + "</head>",
            html,
            count=1,
        )
    else:
        # fallback: просто добавляем в начало
        return css + html


def render_html_template(template_html: str, context: dict) -> str:
    # Используем регулярные выражения для замены плейсхолдеров {key}
    # Это позволяет избежать конфликта с фигурными скобками в CSS
    filled = template_html
    for key, value in context.items():
        if value is None:
            value = ""
        # Заменяем {key} на значение, экранируя специальные символы для regex
        pattern = re.escape(f"{{{key}}}")
        filled = re.sub(pattern, str(value), filled)
    filled = inject_font_css_into_html(filled)
    # добавляем meta charset, если его нет
    if "<meta charset" not in filled.lower():
        if "<head>" in filled.lower():
            filled = re.sub(
                r"(?i)<head>",
                "<head><meta charset=\"utf-8\">",
                filled,
                count=1,
            )
        else:
            filled = "<meta charset=\"utf-8\">" + filled
    return filled


def save_pdf(html_content: str, template_base: Path, output_path: Path):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    HTML(string=html_content, base_url=str(template_base)).write_pdf(str(output_path))


def open_pdf(path: Path):
    system = platform.system().lower()
    if system == "windows":
        os.startfile(str(path))
    elif system == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


# -------- Основной сценарий --------

def main():
    print("=== Генератор PDF чеков (WeasyPrint) ===")

    data_files = list_data_files()
    templates = list_templates()

    print("\nНайденные файлы с данными:")
    if data_files:
        for i, f in enumerate(data_files, 1):
            print(f"  {i}. {f.name}")
    else:
        print("  (нет CSV/JSON в каталоге 'data')")

    print("\nНайденные HTML-шаблоны:")
    if templates:
        for i, t in enumerate(templates, 1):
            print(f"  {i}. {t.name}")
    else:
        print("  (нет шаблонов в каталоге 'templates')")

    if not data_files or not templates:
        print("\nДля работы нужны хотя бы один файл данных и один HTML-шаблон.")
        return

    data_file = choose_from_list("Выберите файл данных", [f.name for f in data_files])
    if data_file is None:
        return
    data_path = DATA_DIR / data_file

    template_file = choose_from_list("Выберите HTML-шаблон", [t.name for t in templates])
    if template_file is None:
        return
    template_path = TEMPLATES_DIR / template_file

    print(f"\nЧтение данных из: {data_path}")
    try:
        records = load_data_file(data_path)
    except Exception as e:
        print(f"Ошибка чтения данных: {e}")
        return

    if not records:
        print("Файл данных пуст или не содержит записей.")
        return

    try:
        invoice_ids, key = extract_invoice_ids(records)
    except Exception as e:
        print(f"Ошибка определения invoice id: {e}")
        return

    if not invoice_ids:
        print("Не найдено ни одного invoice id в данных.")
        return

    chosen_invoice_id = choose_invoice_id(invoice_ids)
    if chosen_invoice_id is None:
        return

    # находим первую запись с выбранным invoice id
    invoice_record = None
    for rec in records:
        if key in rec and str(rec[key]) == chosen_invoice_id:
            invoice_record = rec
            break

    if not invoice_record:
        print("Не удалось найти запись по выбранному invoice id.")
        return

    print(f"\nИспользуем шаблон: {template_path}")
    template_html = read_template(template_path)

    filled_html = render_html_template(template_html, invoice_record)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = re.sub(r"[^\w\-]+", "_", chosen_invoice_id)
    output_name = f"invoice_{safe_id}_{timestamp}.pdf"
    output_path = OUTPUT_DIR / output_name

    print(f"Генерация PDF: {output_path}")
    try:
        save_pdf(filled_html, template_base=template_path.parent, output_path=output_path)
    except Exception as e:
        print(f"Ошибка генерации PDF: {e}")
        return

    print("PDF успешно сохранён.")
    print("Открытие PDF системной программой...")
    try:
        open_pdf(output_path)
    except Exception as e:
        print(f"Не удалось автоматически открыть PDF: {e}")

    print("\nГотово.")


if __name__ == "__main__":
    main()
