def parse_session_metadata(event: dict) -> dict:
    session_id = event.get("session_id", "")
    parts = session_id.split("-")

    store_id = event.get("store_id")
    seller_id = event.get("seller_id")

    if not store_id:
        if len(parts) >= 8:
            store_id = parts[2]
        elif parts:
            store_id = parts[0]
        else:
            store_id = ""

    if seller_id is None:
        if len(parts) >= 8:
            seller_id = "-".join(parts[3:-5])
        else:
            seller_id = "-".join(parts[1:])

    return {
        "store_id": str(store_id),
        "seller_id": seller_id,
    }
