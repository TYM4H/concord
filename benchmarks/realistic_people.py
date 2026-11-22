"""Размеченный миллион для проверки расхождений почты. Все SQL-запросы только на чтение."""
import gzip
import hashlib

from concord.sources.adapters import connect

SIZE = 1_000_000
OVERLAP = 800_000
DUPLICATES = 50_000
# Перестановки исходных номеров в независимых пространствах ID.
KEYS = {"left": (483_023, 79_831), "right": (741_103, 192_337)}
SURNAMES = """Иванов Смирнов Кузнецов Попов Соколов Лебедев Козлов Новиков Морозов Петров
Волков Соловьёв Васильев Зайцев Павлов Семёнов Голубев Виноградов Богданов Воробьёв
Фёдоров Михайлов Беляев Тарасов Белов Комаров Орлов Киселёв Макаров Андреев Ковалёв
Ильин Гусев Титов Кузьмин Кудрявцев Баранов Куликов Алексеев Степанов Яковлев
Сорокин Сергеев Романов Захаров Борисов Королёв Герасимов Пономарёв Григорьев
Лазарев Медведев Ершов Никитин Соболев Рябов Поляков Цветков Данилов Жуков Фролов
Журавлёв Николаев Крылов Максимов Сидоров Осипов Белоусов Федотов Дорофеев Егоров
Матвеев Бобров Дмитриев Калинин Анисимов Петухов Антонов Тимофеев Никифоров
Веселов Филиппов Марков Большаков Суханов Миронов Ширяев Александров Коновалов
Шестаков Казаков Ефимов Денисов Громов Фомин Давыдов Мельников Мартынов Некрасов
Наумов Панов Филатов Сафонов Русаков Устинов Кириллов Тихонов Шубин Рогов Дроздов""".split()
MALE = """Александр Сергей Андрей Дмитрий Алексей Михаил Николай Владимир Иван Евгений
Павел Константин Игорь Олег Роман Артём Максим Юрий Виктор Антон Денис Вадим
Василий Владислав Григорий Даниил Егор Илья Кирилл Леонид Матвей Никита Пётр
Руслан Семён Степан Тимофей Фёдор Ярослав Виталий Борис Валерий Аркадий Георгий""".split()
FEMALE = """Елена Ольга Анна Мария Татьяна Наталья Ирина Екатерина Светлана Юлия
Александра Алиса Алла Алина Анастасия Ангелина Валентина Валерия Варвара Вера
Вероника Виктория Галина Дарья Евгения Елизавета Жанна Зоя Инна Карина Кира
Ксения Лариса Лидия Любовь Людмила Марина Маргарита Надежда Нина Полина София
Ульяна Яна""".split()
PATRONYMICS = """Александров Сергеев Андреев Дмитриев Алексеев Михайлов Николаев
Владимиров Иванов Евгеньев Павлов Константинов Игорев Олегов Романов Артёмов
Максимов Юрьев Викторов Антонов Денисов Вадимов Васильев Владиславов Григорьев
Данилов Егоров Кириллов Леонидов Матвеев Петров Русланов Семёнов Степанов
Тимофеев Фёдоров Ярославов Витальев Борисов Валерьев Аркадьев Георгиев""".split()
MAIL_STEMS = "alex marina sam leo val nick vera kira margo boris mike polina ivan yuri yana anna".split()


def public_id(side, slot):
    factor, bias = KEYS[side]
    raw = ((slot - bias) * pow(factor, -1, SIZE)) % SIZE
    return f"{raw + 1:07d}" if side == "left" else SIZE + raw + 1


def write_truth(path):
    # Оракул знает личность до генерации ФИО/почты. Два разных ID справа могут
    # принадлежать одному человеку. Строки только одной стороны не образуют пары.
    with path.open("xb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as out:
        out.write(b"left_id,right_id\n")
        for person in range(OVERLAP):
            left = public_id("left", person)
            right = public_id("right", person)
            out.write(f"{left},{right}\n".encode())
            if person < DUPLICATES:
                out.write(f"{left},{public_id('right', OVERLAP + person)}\n".encode())
    with path.open("rb") as raw:
        return hashlib.file_digest(raw, "sha256").hexdigest()


def projection_sql(side):
    pg = side == "left"
    table, key = ("registry.people", "person_id") if pg else ("customers", "customer_id")
    idx = "CAST(person_id AS bigint) - 1" if pg else "customer_id - 1000001"
    # Постраничные CTE ограничивают временные таблицы и повторные вычисления хешей.
    def cte(name, query):
        return f"{name} AS MATERIALIZED ({query})" if pg else f"{name} AS ({query} LIMIT 5000)"

    def choose(words, idx):
        items = ", ".join(f"'{w}'" for w in words)
        n = f"CAST(mod({idx}, {len(words)}) + 1 AS integer)" if pg else f"mod({idx}, {len(words)}) + 1"
        return f"(ARRAY[{items}])[{n}]" if pg else f"elt({n}, {items})"

    def hexnum(column, start, length):
        sub = f"substr({column},{start},{length})"
        return (f"CAST(CAST(concat('x',{sub}) AS bit({4*length})) AS bigint)" if pg
                else f"CAST(conv({sub},16,10) AS unsigned)")

    factor, bias = KEYS[side]
    identity = ("slot" if pg else
                "CASE WHEN slot < 800000 THEN slot WHEN slot < 850000 THEN slot-800000 ELSE slot+150000 END")
    missing, typo, renamed = (55, 78, 88) if pg else (110, 182, 205)
    ctes = [
        cte("source_ids", f"SELECT {key} AS id, mod(({idx}) * {factor} + {bias},1000000) AS slot "
            f"FROM {table} WHERE {key} BETWEEN :lo AND :hi"),
        cte("hashes", f"SELECT id, md5(concat('concord-v1-person-', {identity})) AS h, "
            f"md5(concat('concord-v1-{side}-',id)) AS noise FROM source_ids"),
        cte("codes", "SELECT id, " + ", ".join(
            f"{hexnum(col,start,length)} AS {alias}" for col,start,length,alias in [
                ("h",1,4,"s"),("h",5,4,"given"),("h",9,4,"p"),("h",13,4,"flags"),
                ("h",17,8,"mailnum"),("h",25,4,"extra"),("noise",1,4,"en"),
                ("noise",5,4,"nn")]) + " FROM hashes"),
    ]
    sex = "mod(flags,2)"
    # Более частые фамилии и небольшая доля двойных, без кодов и номеров в ФИО.
    surname = choose(SURNAMES, "CASE WHEN mod(extra,5)=0 THEN mod(s,12) ELSE s END")
    second = choose(SURNAMES, "extra")
    surname_suffix = f"CASE WHEN {sex}=0 THEN '' ELSE 'а' END"
    ctes.append(cte("pieces", f"""SELECT id, mod(en,1000) AS en, mod(nn,1000) AS nn,
        concat({surname},{surname_suffix},CASE WHEN mod(flags,17)=0 THEN concat('-', {second},
        {surname_suffix}) ELSE '' END, ' ', CASE WHEN {sex}=0 THEN {choose(MALE,"given")}
        ELSE {choose(FEMALE,"given")} END,' ',{choose(PATRONYMICS,"p")},
        CASE WHEN {sex}=0 THEN 'ич' ELSE 'на' END) AS fio,
        CASE WHEN mod(extra,67)=0 THEN concat('office',mod(mailnum,1500))
        ELSE concat({choose(MAIL_STEMS,"given")},mod(mailnum,1000000)) END AS mailbox,
        CASE WHEN mod(extra,67)=0 THEN 'example.test'
        ELSE {choose(["example.test","inbox.test","mail.test","post.test"],"s")} END AS domain
        FROM codes"""))
    return "WITH " + ", ".join(ctes) + f""" SELECT id,
        CASE WHEN nn < {12 if pg else 28} THEN concat(left(fio,2),substr(fio,4))
             WHEN nn < 150 THEN upper(fio) ELSE fio END AS full_name,
        CASE WHEN en < {missing} THEN NULL
             WHEN en < {typo} THEN concat(mailbox,'@',replace(domain,'.test','.tset'))
             WHEN en < {renamed} THEN concat(mailbox,'.new@',domain)
             ELSE concat(mailbox,'@',domain) END AS email FROM pieces"""


def source_rows(profile, side, password=None):
    from sqlalchemy import text
    query = text(projection_sql(side))
    if profile.driver != {"left":"postgresql","right":"mysql"}[side] or profile.database != "concord_multi_01":
        raise ValueError("Выберите профили тестовых баз concord_multi_01")
    with connect(profile,password) as conn:
        for start in range(0, SIZE, 5000):
            if side == "left":
                bounds = dict(lo=f"{start+1:07d}",hi=f"{start+5000:07d}")
            else:
                bounds = dict(lo=SIZE+start+1,hi=SIZE+start+5000)
            with conn.execute(query,bounds) as rows:
                count = 0
                for row in rows.mappings():
                    count += 1
                    yield dict(row)
                if count != 5000:
                    raise ValueError(f"Неполный диапазон ID {side} {start}")
