# 2GIS Platform — автоматическая регистрация и демо-ключи

Скрипт для тестирования [2ГИС Platform](https://platform.2gis.ru/ru): регистрация аккаунта, подтверждение почты, создание компании и выпуск **демо API-ключа** (UUID, срок ~30 дней).

## Что делает

1. Берёт временную Gmail через [SmailPro](https://smailpro.com) (Real Gmail).
2. Открывает `platform.2gis.ru` в **родном Google Chrome** (инкогнито, CDP).
3. Регистрируется: ФИО, телефон `+7…`, почта, пароль.
4. Ждёт **6-значный код** из письма и вводит его.
5. Создаёт компанию с произвольным названием.
6. Нажимает «Создать демо-ключ» и сохраняет UUID.

## Требования

- macOS (путь к Chrome захардкожен под macOS)
- Python 3.11+
- Google Chrome
- Аккаунт SmailPro Premium + файл `smailpro_cookies.json` (куки после логина на smailpro.com)

## Установка

```bash
pip install -r requirements.txt
playwright install chromium   # для connect_over_cdp достаточно драйвера
```

Положите `smailpro_cookies.json` в каталог со скриптом (не коммитьте в git).

## Запуск

```bash
# один ключ
python3 register_2gis.py --count 1

# пять ключей без вопросов
python3 register_2gis.py --count 5 -y

# только до формы регистрации
python3 register_2gis.py --count 1 --dry-run
```

Без `--count` спросит, сколько ключей сделать.

## Результат

| Файл | Содержимое |
|------|------------|
| `2gis-keys.jsonl` | Полные записи: email, password, company, key (UUID), key_url |
| `2gis-demo-keys.json` | Массив `keys` + `accounts` для удобного чтения ботом |
| `2gis_book.json` | Книга SmailPro-ящиков (отдельно от других проектов) |
| `runs/*.log` | Лог сессии |

Права на файлы с ключами: `600`.

## Для Grok / агента

После успешного прогона возьмите ключ из:

```bash
python3 -c "import json; d=json.load(open('2gis-demo-keys.json')); print(d['keys'][-1])"
```

Или последнюю строку `2gis-keys.jsonl` — поле `"key"`.

## Ограничения

- Демо-ключ один на аккаунт, блокируется примерно через месяц.
- SmailPro без Premium/кук может просить капчу — скрипт откроет Chrome и подождёт.
- Скрипт для **тестирования** продукта 2ГИС, не для обхода лимитов сервиса.

## Лицензия

MIT — используйте на свой риск, только в рамках разрешённого тестирования.
