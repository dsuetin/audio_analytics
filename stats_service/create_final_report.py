from pathlib import Path

import pandas as pd


INPUT_FILE = "reports/transcript_report_2026-07-17.xlsx"
OUTPUT_FILE = "reports/final_transcript_report_2026-07-17.xlsx"


GREETINGS = (
    "здравствуйте",
    "добрый день",
    "добрый вечер",
    "доброе утро",
    "привет",
    "приветствую",
)

FAREWELLS = (
    "до свидания",
    "до свиданья",
    "всего доброго",
    "всего хорошего",
    "до встречи",
    "счастливо",
    "хорошего дня",
    "хорошего вечера",
    "заходите еще",
)


def last_not_empty(series):
    series = series.dropna()
    if len(series) == 0:
        return None

    series = series.astype(str)
    series = series[series.str.strip() != ""]
    if len(series) == 0:
        return None

    return series.iloc[-1]


def merge_text(series):
    parts = []
    for text in series:
        if pd.isna(text):
            continue
        text = str(text).strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def contains_any(text: str, phrases) -> bool:
    if pd.isna(text):
        return False
    text = str(text).lower()
    return any(phrase in text for phrase in phrases)


def first_match_index(texts, phrases):
    for i, text in enumerate(texts):
        if contains_any(text, phrases):
            return i
    return None


def last_match_index(texts, phrases):
    for i in range(len(texts) - 1, -1, -1):
        if contains_any(texts[i], phrases):
            return i
    return None


def to_bool_series(series: pd.Series) -> pd.Series:
    """
    Надежно приводит столбец к bool, даже если там строки True/False.
    """
    if series.dtype == bool:
        return series.fillna(False)

    return (
        series.astype(str)
        .str.lower()
        .map(lambda x: x in ("true", "1", "yes", "y", "t"))
        .fillna(False)
    )


def process_sheet(store_id: str, df: pd.DataFrame) -> list[dict]:
    if "created_at" in df.columns:
        df = df.copy()
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
        df = df.sort_values("created_at", kind="stable")

    if "client_id" not in df.columns:
        raise KeyError(f"'client_id' column is missing in sheet {store_id}")

    df = df.copy()

    # Протягиваем client_id вниз, чтобы строки без явного client_id тоже попали в нужного клиента
    df["client_id_effective"] = df["client_id"].ffill()
    df = df[df["client_id_effective"].notna()].copy()

    result = []

    for client_id, group in df.groupby("client_id_effective", sort=False):
        group = group.sort_values("created_at", kind="stable").reset_index(drop=True)

        texts = group["recognition_text"].fillna("").astype(str).tolist()

        # Для времени считаем маркеры приветствия/прощания
        start_idx = first_match_index(texts, GREETINGS)
        end_idx = last_match_index(texts, FAREWELLS)

        if start_idx is not None and end_idx is not None and end_idx >= start_idx:
            dialog_start_at = group.iloc[start_idx]["created_at"]
            dialog_end_at = group.iloc[end_idx]["created_at"]
        else:
            # fallback, если маркеры не найдены или некорректны
            dialog_start_at = group["created_at"].iloc[0] if "created_at" in group.columns else None
            dialog_end_at = group["created_at"].iloc[-1] if "created_at" in group.columns else None

        dialog_duration_sec = None
        if pd.notna(dialog_start_at) and pd.notna(dialog_end_at):
            dialog_duration_sec = (dialog_end_at - dialog_start_at).total_seconds()

        # ВАЖНО:
        # Текст и флаги берем по ВСЕМУ клиенту, а не по обрезанному окну,
        # иначе можно потерять поздние alarm/sale записи (как у client_42).
        sale_series = to_bool_series(group["is_sale"]) if "is_sale" in group.columns else pd.Series([False] * len(group))
        alarm_series = to_bool_series(group["is_alarm_triggered"]) if "is_alarm_triggered" in group.columns else pd.Series([False] * len(group))

        result.append(
            {
                "store_id": store_id,
                "client_id": client_id,
                "dialog_start_at": dialog_start_at,
                "dialog_end_at": dialog_end_at,
                "dialog_duration_sec": dialog_duration_sec,
                "seller_id": last_not_empty(group["seller_id"]) if "seller_id" in group.columns else None,
                "dialog_type": last_not_empty(group["dialog_type"]) if "dialog_type" in group.columns else None,
                "recognition_text": merge_text(group["recognition_text"]) if "recognition_text" in group.columns else "",
                "is_sale": bool(sale_series.any()),
                "is_alarm_triggered": bool(alarm_series.any()),
            }
        )

    return result


def main():
    sheets = pd.read_excel(INPUT_FILE, sheet_name=None)

    all_rows = []

    for store_id, df in sheets.items():
        all_rows.extend(process_sheet(store_id, df))

    out = pd.DataFrame(all_rows)

    cols = [
        "store_id",
        "client_id",
        "dialog_start_at",
        "dialog_end_at",
        "dialog_duration_sec",
        "seller_id",
        "dialog_type",
        "recognition_text",
        "is_sale",
        "is_alarm_triggered",
    ]
    out = out[[c for c in cols if c in out.columns]]

    out.to_excel(OUTPUT_FILE, index=False)

    print(f"Wrote {OUTPUT_FILE}")
    print(f"Clients: {len(out)}")


if __name__ == "__main__":
    main()