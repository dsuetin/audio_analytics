import pandas as pd
import snowballstemmer

stemmer = snowballstemmer.stemmer("russian")

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

LOSS_SCENARIOS = {

    "Не устроила цена": [
        "поищу подешевле",
        "другом магазине дешевле",
        "все дорого",
        "всё дорого",
        "нет столько денег",
        "нет денег",
        "нет таких денег",
        "нужно посоветоваться",
        "я подумаю",
        "надо посоветоваться",
        "на другую сумму рассчитывал",
        "поеду посмотрю еще",
        "если что вернусь",
        "прочитаю отзывы",
        "до зарплаты",
        "пенсия",
        "через пару месяцев",
        "через пару недель",
        "через пару дней",
        "потом приеду",
        "хочу поискать",
    ],


    "В магазине не оказалось нужного количества или модели": [
        "где найти",
        "у вас нет",
        "нету нужной",
        "когда будет в наличии",
        "где еще может продаваться",
        "заказать можете",
        "можете заказать",
        "мне нужна именно",
        "заказать со склада",
        "нет в наличии",
        "посмотрю по программе",
        "позвоню на другой магазин",
        "посмотрю на ближайшем",
        "можем привезти",
        "хороший аналог",
        "есть на складе",
        "покажу варианты",
        "проверю по программе",
    ],


    "Получил консультацию/провел мониторинг цен/ассортимента и ушел": [
        "подумаю",
        "подумать",
        "присматриваю",
        "покупать не буду",
        "спасибо за консультацию",
        "просто хотел цены посмотреть",
        "у ваших конкурентов",
        "у конкурентов",
        "не буду брать",
        "прицениваюсь",
        "прицениться",
        "после зарплаты",
        "просто смотрю",
        "деньги не брал",
        "просто посмотреть",
        "приеду позже",
        "буду иметь в виду",
        "посмотрю еще",
    ],
}



def normalize_text(text: str):
    words = (
        str(text)
        .lower()
        .replace(",", " ")
        .replace(".", " ")
        .split()
    )

    return stemmer.stemWords(words)


def normalize_phrase(phrase: str):
    return " ".join(
        stemmer.stemWords(
            phrase.lower().split()
        )
    )


def contains_phrase(text: str, phrase: str):

    text_words = normalize_text(text)
    phrase_words = normalize_phrase(phrase).split()

    if len(phrase_words) > len(text_words):
        return False

    for i in range(len(text_words) - len(phrase_words) + 1):

        window = text_words[
            i:i + len(phrase_words)
        ]

        if window == phrase_words:
            return True

    return False


LOSS_SCENARIOS_LEMMA = {
    name: [
        normalize_phrase(p)
        for p in phrases
    ]
    for name, phrases in LOSS_SCENARIOS.items()
}


def detect_loss_reason(text: str, is_sale: bool):

    if is_sale:
        return "Покупка"


    for scenario, phrases in LOSS_SCENARIOS_LEMMA.items():

        for phrase in phrases:

            if contains_phrase(
                text,
                phrase,
            ):
                return scenario


    return None


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

        #
        # После последнего "до свидания" все отбрасываем
        #
        if end_idx is not None:

            group_for_report = (
                group.iloc[: end_idx + 1]
                .copy()
                .reset_index(drop=True)
            )

        else:

            group_for_report = (
                group.copy()
                .reset_index(drop=True)
            )


        merged_text = merge_text(
            group_for_report["recognition_text"]
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
                group_for_report["created_at"].iloc[0]
                if "created_at" in group_for_report.columns
                else None
            )

            dialog_end_at = (
                group_for_report["created_at"].iloc[-1]
                if "created_at" in group_for_report.columns
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
            to_bool_series(group_for_report["is_sale"])
            if "is_sale" in group_for_report.columns
            else pd.Series([False] * len(group_for_report))
        )

        alarm_series = (
            to_bool_series(group_for_report["is_alarm_triggered"])
            if "is_alarm_triggered" in group_for_report.columns
            else pd.Series([False] * len(group_for_report))
        )


        session_ids = []

        if "session_id" in group.columns:

            session_ids = (
                group_for_report["session_id"]
                .dropna()
                .astype(str)
                .unique()
                .tolist()
            )


        is_sale = bool(
            sale_series.any()
        )


        # определяем причину ухода
        loss_reason = None

        if (
            group["dialog_type"].astype(str)
            .str.lower()
            .eq("buy")
            .any()
        ):

            loss_reason = detect_loss_reason(
                merged_text, is_sale
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

                "recognition_text": merged_text,

                "dialog_type": (
                    last_not_empty(
                        group["dialog_type"]
                    )
                    if "dialog_type" in group.columns
                    else None
                ),

                "is_sale": is_sale,

                "loss_reason": loss_reason,

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


def create_final_report(input_file: str, output_file: str):

    sheets = pd.read_excel(
        input_file,
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

        "loss_reason",

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

        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
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
        f"Wrote {output_file}"
    )

    print(
        f"Clients: {len(out)}"
    )
    return output_file


def main():
    create_final_report(
        INPUT_FILE,
        OUTPUT_FILE,
    )



if __name__ == "__main__":
    main()