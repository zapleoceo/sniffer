-- Стартовый набор очереди вступлений: 36 групп из docs/chats-nha-trang.md —
-- 35 нячангских, снятых юзерботом через contacts.SearchRequest, плюс одна
-- общевьетнамская барахолка, названная владельцем напрямую.
--
-- Почему сид именно SQL, а не разовая команда и не парсинг документа.
-- Ручное вступление отменено (roadmap, волна 4.5), значит стартовый набор —
-- это состояние системы, а состояние сервера определяется репозиторием
-- (CLAUDE.md, CI/CD): разовая команда на сервере была бы ровно той ручной
-- правкой, которую правило запрещает. Каталог docker-entrypoint-initdb.d уже
-- есть и уже прогоняет 001_init.sql, так что новому механизму взяться
-- неоткуда. Парсить сам docs/chats-nha-trang.md в рантайме нельзя: проза
-- станет зависимостью кода и сломается на первой же правке таблицы.
--
-- Идемпотентен: ON CONFLICT DO NOTHING. Повторный прогон на живой базе
-- ничего не сдвинет — ни уже разобранных кандидатов, ни отклонённых.
--
-- Порядок из документа сохранён приоритетом: волна 1 (байки и общая
-- барахолка) 10..16, чат владельца 17, волна 2 (жильё) 20..28, волна 3 (общие
-- чаты) 30..48.
-- Очередь разбирается по десять чатов в скользящие сутки, то есть полностью — за четыре
-- дней, и порядок решает, что появится в выдаче на первой неделе.

INSERT INTO chat_candidates (key, username, found_in, priority) VALUES
    ('@auto_moto_vietnam', 'auto_moto_vietnam', 'seed:wave1', 10),
    ('@nyachang_uslugi', 'nyachang_uslugi', 'seed:wave1', 11),
    ('@nha_trang_kupi_proday', 'Nha_Trang_kupi_proday', 'seed:wave1', 12),
    ('@barakholka_nyachang', 'barakholka_nyachang', 'seed:wave1', 13),
    ('@baraholka_nachang', 'baraholka_nachang', 'seed:wave1', 14),
    ('@nyachang_barakholka', 'nyachang_barakholka', 'seed:wave1', 15),
    ('@t2tnhatrangchat2', 'T2TNhaTrangChat2', 'seed:wave1', 16),
    -- Общевьетнамская барахолка, названа владельцем 01.09.2026. Приоритет
    -- между волнами: она плотная, но объявления в ней со всей страны, поэтому
    -- нячангские чаты волны 1 разбираются раньше.
    ('@vietavito', 'vietavito', 'seed:owner', 17),
    ('@arenda_v_nyachang', 'arenda_v_nyachang', 'seed:wave2', 20),
    ('@nyachang_arendaa', 'nyachang_arendaa', 'seed:wave2', 21),
    ('@arenda_nychang', 'Arenda_nychang', 'seed:wave2', 22),
    ('@arenda_nyachangg', 'Arenda_Nyachangg', 'seed:wave2', 23),
    ('@nyachang_arendavn', 'nyachang_arendavn', 'seed:wave2', 24),
    ('@niachang_appart', 'niachang_appart', 'seed:wave2', 25),
    ('@zhilyo_nyachang18', 'Zhilyo_nyachang18', 'seed:wave2', 26),
    ('@nhatrang_pro_house', 'nhatrang_pro_house', 'seed:wave2', 27),
    ('@nhatrangapartment', 'nhatrangapartment', 'seed:wave2', 28),
    ('@nyachang_chatask', 'nyachang_chatask', 'seed:wave3', 30),
    ('@nha_trang_1', 'Nha_Trang_1', 'seed:wave3', 31),
    ('@nhatrang_bg', 'nhatrang_bg', 'seed:wave3', 32),
    ('@vietnam_chat1', 'vietnam_chat1', 'seed:wave3', 33),
    ('@nyachang11', 'nyachang11', 'seed:wave3', 34),
    ('@nyachang_pro', 'nyachang_pro', 'seed:wave3', 35),
    ('@nyachang1', 'nyachang1', 'seed:wave3', 36),
    ('@nhatrangchat', 'NhaTrangchat', 'seed:wave3', 37),
    ('@vietnamnha', 'vietnamnha', 'seed:wave3', 38),
    ('@nyachangs', 'nyachangs', 'seed:wave3', 39),
    ('@vietnam_arenda', 'Vietnam_arenda', 'seed:wave3', 40),
    ('@chatt_nyachang', 'chatt_nyachang', 'seed:wave3', 41),
    ('@chat_nyachang', 'chat_nyachang', 'seed:wave3', 42),
    ('@nha_trang_live', 'nha_trang_live', 'seed:wave3', 43),
    ('@barakholka_nhatrang', 'Barakholka_NhaTrang', 'seed:wave3', 44),
    ('@niachang_baraxlanet', 'niachang_baraxlanet', 'seed:wave3', 45),
    ('@chat_nyachang2', 'chat_nyachang2', 'seed:wave3', 46),
    ('@ads_nhatrang', 'ads_nhatrang', 'seed:wave3', 47),
    ('@sales_vietnam', 'sales_vietnam', 'seed:wave3', 48)
ON CONFLICT (key) DO NOTHING;

-- Дананг и общевьетнамские чаты из того же документа. Пишем их сразу в
-- отклонённые, а не пропускаем молча: иначе разведка встретит ссылку на
-- @danang001 в первом же чате и потратит на неё resolve_username — и так
-- при каждой встрече. Причина оставлена словом: при расширении на второй
-- город (roadmap, P3) эти строки станут стартовым набором для него.
INSERT INTO chat_rejects (key, reason) VALUES
    ('@danang001', 'foreign_city'),
    ('@danang_barakholka', 'foreign_city'),
    ('@arenda_vetnam', 'foreign_city'),
    ('@vetnam_arenda', 'foreign_city')
ON CONFLICT (key) DO NOTHING;
