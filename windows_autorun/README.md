# Audio Analytics — Client

Микрофон → gRPC → сервер → Kafka → распознавание. Автозапуск при входе в Windows.

---

## Установка

### 1. Скопируйте папку на компьютер магазина

Рекомендуемое расположение: `C:\AudioAnalytics`.

### 2. Заполните `.env`

```bat
copy C:\AudioAnalytics\.env.example C:\AudioAnalytics\.env
```

| Параметр | Значение |
|---|---|
| `SERVER_IP` | IP сервера |
| `STORE_ID` | ID магазина |
| `WORKER_NAME` | имя сотрудника |
| `AUDIO_DEVICE` | оставить пустым |
| `AUDIO_DEVICE_ID` | ID внешнего USB-микрофона (см. ниже) |

### 3. Запустите установку

```bat
C:\AudioAnalytics\start.bat
```

Скрипт сам: создаёт `.venv`, ставит зависимости, регистрирует автозапуск, запускает клиент.

---

## Выбор микрофона

Для внешнего USB-микрофона используйте `AUDIO_DEVICE_ID` (аппаратный ID не меняется при переподключении).

```bat
C:\AudioAnalytics\.venv\Scripts\python.exe C:\AudioAnalytics\list_devices.py
```

Скопируйте ID до первого `\` в `.env`:

```
AUDIO_DEVICE_ID=USB\VID_046D&PID_081B&MI_02
```

Если `AUDIO_DEVICE_ID` и `AUDIO_DEVICE` пусты — используется микрофон по умолчанию.

---

## Обслуживание

| Действие | Команда |
|---|---|
| Запуск / перезапуск | `start.bat` |
| Остановка | `stop.bat` |
| Проверка процесса | `tasklist /FI "IMAGENAME eq pythonw.exe"` |
| Просмотр лога | `Get-Content C:\AudioAnalytics\logs\client.log -Wait -Tail 20` |

---

## Деинсталляция

```bat
C:\AudioAnalytics\uninstall.bat
```

Удаляет автозапуск, останавливает процессы, удаляет `.venv`, `logs`, `__pycache__`. Исходный код сохраняется.
