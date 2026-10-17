"""
Fixed list of all store names found in transcripts table.
This is used for the dropdown selection in the audio download UI.
Updated from: Названия Магазинов.xlsx

Includes mapping between full store names and truncated versions (31 chars)
found in historical Excel reports due to sheet name limitations.
"""

STORES = [
    "г_Армавир_ул_Советской_Армии_177",
    "г_Армавир_ул_Советской_Армии_378",
    "г_Ессентуки_ул_Володарского_27",
    "г_Ессентуки_ул_Октябрьская_294",
    "г_Ессентуки_ул_Первомайская_85",
    "г_Кисловодск_пр-кт_Победы_98",
    "г_Кисловодск_ул_40_лет_Октября_37",
    "г_Кисловодск_ул_Чапаева_71А",
    "г_Лермонтов_пр_Лермонтова_д17",
    "г_Минеральные_Воды_ул_Гагарина_73_А",
    "г_Минеральные_Воды_ул_Московская_д_29_6",
    "г_Пятигорск_Бештаугорское_шоссе_4",
    "г_Пятигорск_ул_Калинина_299",
    "г_Пятигорск_ул_Коллективная_26А",
    "г_Пятигорск_ул_Мира_201а_строение_1",
    "г_Пятигорск_ул_Первомайская_34",
    "г_Ставрополь_ул_50_лет_ВЛКСМ_43_А",
    "г_Ставрополь_ул._Доваторцев_62_А",
    "п_Иноземцево_ул_Гагарина_2Е_(УТЭП)",
    "ст_Ессентукская_ул_Гагарина_5Д",
]

STORE_TRUNCATION_LENGTH = 31

STORE_TO_TRUNCATED = {
    "г_Армавир_ул_Советской_Армии_177": "г_Армавир_ул_Советской_Армии_17",
    "г_Армавир_ул_Советской_Армии_378": "г_Армавир_ул_Советской_Армии_37",
    "г_Ессентуки_ул_Володарского_27": "г_Ессентуки_ул_Володарского_27",
    "г_Ессентуки_ул_Октябрьская_294": "г_Ессентуки_ул_Октябрьская_294",
    "г_Ессентуки_ул_Первомайская_85": "г_Ессентуки_ул_Первомайская_85",
    "г_Кисловодск_пр-кт_Победы_98": "г_Кисловодск_пр-кт_Победы_98",
    "г_Кисловодск_ул_40_лет_Октября_37": "г_Кисловодск_ул_40_лет_Октября_",
    "г_Кисловодск_ул_Чапаева_71А": "г_Кисловодск_ул_Чапаева_71А",
    "г_Лермонтов_пр_Лермонтова_д17": "г_Лермонтов_пр_Лермонтова_д17",
    "г_Минеральные_Воды_ул_Гагарина_73_А": "г_Минеральные_Воды_ул_Гагарина_",
    "г_Минеральные_Воды_ул_Московская_д_29_6": "г_Минеральные_Воды_ул_Московска",
    "г_Пятигорск_Бештаугорское_шоссе_4": "г_Пятигорск_Бештаугорское_шоссе",
    "г_Пятигорск_ул_Калинина_299": "г_Пятигорск_ул_Калинина_299",
    "г_Пятигорск_ул_Коллективная_26А": "г_Пятигорск_ул_Коллективная_26А",
    "г_Пятигорск_ул_Мира_201а_строение_1": "г_Пятигорск_ул_Мира_201а_строен",
    "г_Пятигорск_ул_Первомайская_34": "г_Пятигорск_ул_Первомайская_34",
    "г_Ставрополь_ул_50_лет_ВЛКСМ_43_А": "г_Ставрополь_ул_50_лет_ВЛКСМ_43",
    "г_Ставрополь_ул._Доваторцев_62_А": "г_Ставрополь_ул._Доваторцев_62_",
    "п_Иноземцево_ул_Гагарина_2Е_(УТЭП)": "п_Иноземцево_ул_Гагарина_2Е_(УТ",
    "ст_Ессентукская_ул_Гагарина_5Д": "ст_Ессентукская_ул_Гагарина_5Д",
}

TRUNCATED_TO_STORE = {v: k for k, v in STORE_TO_TRUNCATED.items()}


def get_all_stores() -> list[str]:
    """Return the fixed list of all known stores."""
    return STORES.copy()


def get_truncated_store(full_store: str) -> str:
    """Get truncated version of store name (31 chars) as stored in historical Excel reports."""
    return STORE_TO_TRUNCATED.get(full_store, full_store[:STORE_TRUNCATION_LENGTH])


def get_full_store(truncated_store: str) -> str | None:
    """Get full store name from truncated version."""
    return TRUNCATED_TO_STORE.get(truncated_store)


def normalize_store_for_comparison(store_id: str) -> str:
    """
    Normalize store_id for comparison with historical data.
    Returns truncated version if store is longer than 31 chars.
    """
    if len(store_id) > STORE_TRUNCATION_LENGTH:
        return store_id[:STORE_TRUNCATION_LENGTH]
    return store_id


def normalize_store_id(store_id: str) -> str:
    """
    Normalize store_id for comparison.
    Handles variations like 'ул.' vs 'ул' (with/without dot).
    """
    # Replace 'ул.' with 'ул' for consistent comparison
    normalized = store_id.replace('ул.', 'ул')
    return normalized


def match_store_id(report_store_id: str, filter_store_id: str) -> bool:
    """
    Check if a store_id from report matches the filter store_id.
    Handles both full and truncated store names, as well as variations like 'ул.' vs 'ул'.
    
    CRITICAL: Does NOT allow ambiguous matches. If a truncated store_id
    could match multiple full store names (e.g., "...37" could be "...37" or "...378"),
    returns False to prevent false positives.
    
    Args:
        report_store_id: store_id from the Excel report (may be truncated)
        filter_store_id: store_id from filter (full name from stores.py)
    
    Returns:
        True if they match unambiguously
    """
    # Exact match always wins
    if report_store_id == filter_store_id:
        return True
    
    # Try normalized match (handles 'ул.' vs 'ул' variations)
    if normalize_store_id(report_store_id) == normalize_store_id(filter_store_id):
        return True
    
    # Try to resolve truncated report_store_id to full name
    full_from_truncated = get_full_store(report_store_id)
    if full_from_truncated and full_from_truncated == filter_store_id:
        return True
    
    # Check if filter_store_id truncated matches report_store_id
    # BUT only if this is unambiguous (no other store would also match)
    truncated = get_truncated_store(filter_store_id)
    if report_store_id == truncated:
        # Verify this is unambiguous: the truncated form should resolve back to filter_store_id
        resolved = get_full_store(truncated)
        if resolved == filter_store_id:
            return True
        # Ambiguous match - do not allow
        # (e.g., "...37" could be "...37" or "...378")
    
    return False
