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

def first_not_empty(series):
    series = series.dropna()

    if len(series) == 0:
        return None

    series = series.astype(str)
    series = series[series.str.strip() != ""]

    if len(series) == 0:
        return None

    return series.iloc[0]


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

        text = " ".join(str(text).split())

        if text:
            parts.append(text + " ")

    return "\n".join(parts)

def contains_any(text: str, phrases) -> bool:
    if pd.isna(text):
        return False

    text = str(text).lower()

    return any(
        phrase in text
        for phrase in phrases
    )


def first_match_index(texts, phrases):

    for i, text in enumerate(texts):

        if contains_any(text, phrases):
            return i

    return None


def last_match_index(texts, phrases):

    for i in range(len(texts)-1, -1, -1):

        if contains_any(texts[i], phrases):
            return i

    return None


def to_bool_series(series: pd.Series) -> pd.Series:

    if series.dtype == bool:
        return series.fillna(False)

    return (
        series.astype(str)
        .str.lower()
        .map(
            lambda x: x in (
                "true",
                "1",
                "yes",
                "y",
                "t",
            )
        )
        .fillna(False)
    )


def process_sheet(store_id: str, df: pd.DataFrame):

    if "created_at" in df.columns:

        df = df.copy()

        df["created_at"] = pd.to_datetime(
            df["created_at"],
            errors="coerce",
        )

        df = df.sort_values(
            "created_at",
            kind="stable",
        )


    if "client_id" not in df.columns:
        raise KeyError(
            f"'client_id' column is missing in sheet {store_id}"
        )


    df = df.copy()


    # сохраняем логику рабочего варианта
    df["client_id_effective"] = (
        df["client_id"]
        .ffill()
    )


    df = df[
        df["client_id_effective"].notna()
    ].copy()


    result = []


    for client_id, group in df.groupby(
        "client_id_effective",
        sort=False,
    ):

        group = group.sort_values(
            "created_at",
            kind="stable",
        ).reset_index(drop=True)


        texts = (
            group["recognition_text"]
            .fillna("")
            .astype(str)
            .tolist()
        )


        start_idx = first_match_index(
            texts,
            GREETINGS,
        )


        end_idx = last_match_index(
            texts,
            FAREWELLS,
        )


        if (
            start_idx is not None
            and end_idx is not None
            and end_idx >= start_idx
        ):

            dialog_start_at = (
                group.iloc[start_idx]["created_at"]
            )

            dialog_end_at = (
                group.iloc[end_idx]["created_at"]
            )

        else:

            dialog_start_at = (
                group["created_at"].iloc[0]
                if "created_at" in group.columns
                else None
            )

            dialog_end_at = (
                group["created_at"].iloc[-1]
                if "created_at" in group.columns
                else None
            )


        dialog_duration_sec = None

        if (
            pd.notna(dialog_start_at)
            and pd.notna(dialog_end_at)
        ):

            dialog_duration_sec = (
                dialog_end_at - dialog_start_at
            ).total_seconds()



        sale_series = (
            to_bool_series(group["is_sale"])
            if "is_sale" in group.columns
            else pd.Series([False] * len(group))
        )


        alarm_series = (
            to_bool_series(group["is_alarm_triggered"])
            if "is_alarm_triggered" in group.columns
            else pd.Series([False] * len(group))
        )


        # новые данные
        session_ids = []

        if "session_id" in group.columns:

            session_ids = (
                group["session_id"]
                .dropna()
                .astype(str)
                .unique()
                .tolist()
            )


        result.append(
            {

                "store_id": store_id,

                "seller_id": (
                    first_not_empty(group["seller_id"])
                    if "seller_id" in group.columns
                    else None
                ),

                "client_id": client_id,


                "recognition_text": (
                    merge_text(
                        group["recognition_text"]
                    )
                    if "recognition_text" in group.columns
                    else ""
                ),


                "dialog_type": (
                    last_not_empty(
                        group["dialog_type"]
                    )
                    if "dialog_type" in group.columns
                    else None
                ),


                "is_sale": bool(
                    sale_series.any()
                ),


                "is_alarm_triggered": bool(
                    alarm_series.any()
                ),


                "dialog_start_at": dialog_start_at,

                "dialog_end_at": dialog_end_at,

                "dialog_duration_sec": dialog_duration_sec,


                "session_ids": ", ".join(
                    session_ids
                ),

            }
        )


    return result



def main():

    sheets = pd.read_excel(
        INPUT_FILE,
        sheet_name=None,
    )


    all_rows = []


    for store_id, df in sheets.items():

        print(
            f"Processing sheet: {store_id}"
        )

        all_rows.extend(
            process_sheet(
                store_id,
                df,
            )
        )


    out = pd.DataFrame(
        all_rows
    )


    cols = [

        "store_id",

        "seller_id",

        "client_id",

        "recognition_text",

        "dialog_type",

        "is_sale",

        "is_alarm_triggered",

        "dialog_start_at",

        "dialog_end_at",

        "dialog_duration_sec",

        "session_ids",

    ]


    out = out[
        [
            c
            for c in cols
            if c in out.columns
        ]
    ]


    # убираем timezone, иначе Excel падает
    for col in [
        "dialog_start_at",
        "dialog_end_at",
    ]:

        if col in out.columns:

            out[col] = (
                pd.to_datetime(
                    out[col],
                    errors="coerce",
                )
                .dt.tz_localize(None)
            )

        with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
            out.to_excel(
                writer,
                index=False,
                sheet_name="report",
            )

            ws = writer.sheets["report"]

            for row in ws.iter_rows():
                for cell in row:
                    cell.alignment = cell.alignment.copy(
                        wrap_text=True
                    )


    print(
        f"Wrote {OUTPUT_FILE}"
    )

    print(
        f"Clients: {len(out)}"
    )



if __name__ == "__main__":
    main()