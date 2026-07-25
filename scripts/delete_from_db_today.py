from datetime import date
import psycopg2

HOST = "192.168.0.10"
PORT = 5432
DATABASE = "speech_db"
USER = "speech"
PASSWORD = "speech"  # замените на свой пароль

TABLE_NAME = "transcripts"      # замените на нужную таблицу
DATE_COLUMN = "created_at"   # замените на нужное поле с датой

conn = psycopg2.connect(
    host=HOST,
    port=PORT,
    dbname=DATABASE,
    user=USER,
    password=PASSWORD,
)

try:
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {TABLE_NAME}
                WHERE {DATE_COLUMN}::date = %s
                """,
                (date.today(),),
            )
            print(f"Удалено строк: {cur.rowcount}")
finally:
    conn.close()