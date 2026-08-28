from __future__ import annotations


def assign_display_client_ids(
    rows: list[dict],
    identity=None,
) -> list[dict]:
    """Формирует display client_id: client_1, client_2, ... отдельной
    последовательностью для КАЖДОГО магазина, в порядке первого появления
    клиента (ключа identity).

    - identity(row) -> str: как отличать разных клиентов.
      По умолчанию: текущее значение "client_id" ряда (до перезаписи).
      В отчёте, где 1 строка = 1 отдельный разговор, передают уникальный
      ключ на разговор, чтобы два разных разговора с одинаковым техническим ID
      (realtime-счётчик сбрасывается при каждом рестарте процесса) получали
      РАЗНЫЕ display-номера.
    - В пределах одного магазина display-номера всегда уникальны и идут
      подряд, начиная с client_1.
    - Если row уже содержит "internal_client_id" — он сохраняется как есть.
      Иначе в "internal_client_id" записывается исходный "client_id" ряда.
    - Ряды без ключа identity сохраняют исходный "client_id"
      и получают internal_client_id = None.

    Входные строки не изменяются; возвращается новый список.
    """
    if identity is None:
        def identity(row: dict) -> str:
            value = row.get("client_id")
            return str(value).strip() if value is not None else ""

    store_order: dict[str, list[str]] = {}
    for row in rows:
        key = identity(row)
        if not key:
            continue
        store = str(row.get("store_id") or "").strip()
        ordered = store_order.setdefault(store, [])
        if key not in ordered:
            ordered.append(key)

    mapping: dict[tuple[str, str], int] = {}
    for store, entries in store_order.items():
        for n, entry in enumerate(entries, start=1):
            mapping[(store, entry)] = n

    result = []
    for row in rows:
        new_row = dict(row)
        key = identity(row)
        if key:
            store = str(row.get("store_id") or "").strip()
            if new_row.get("internal_client_id") is None:
                original = row.get("client_id")
                new_row["internal_client_id"] = (
                    str(original).strip() if original is not None else None
                )
            new_row["client_id"] = f"client_{mapping[(store, key)]}"
        else:
            new_row["internal_client_id"] = None
        result.append(new_row)

    return result
