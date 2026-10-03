"""Дананг: районы, варды и ЖК-ориентиры — данные для `facts_place`.

Зачем Дананг в справочнике Нячанга: нячангские чаты и барахолки постят объявления и из
Дананга, и без справочника такое объявление числится нячангским. В замере 03.10.2026 под
`city=nha_trang` лежало 410 карточек с названиями районов Дананга (Ngũ Hành Sơn, Sơn Trà,
Hải Châu, Hòa Xuân…), из них 260 вовсе без названия города. Справочник их узнаёт.

Зон (север/центр/юг/запад) у Дананга нет: это схема нячангских агентств, у Дананга другая
география, и называть его районы севером или югом значило бы выдумывать. Формат записи и
правила написаний — как у `districts_nha_trang`.
"""

from __future__ import annotations

DA_NANG: tuple[tuple[str, ...], ...] = (
    ("hai_chau", "Hải Châu", "", "area", "hai chau", "хай тяу", "хайтяу"),
    ("son_tra", "Sơn Trà", "", "area", "son tra", "шон тра", "сон тра"),
    (
        "ngu_hanh_son", "Ngũ Hành Sơn", "", "area",
        "ngu hanh son", "нгу хань шон", "нгу хань сон", "нгуханьшон",
    ),
    ("thanh_khe", "Thanh Khê", "", "area", "thanh khe", "тхань кхе"),
    ("lien_chieu", "Liên Chiểu", "", "area", "lien chieu", "льен тьеу"),
    ("cam_le", "Cẩm Lệ", "", "area", "cam le", "кам ле"),
    ("hoa_vang", "Hòa Vang", "", "area", "hoa vang"),
    ("my_khe", "Mỹ Khê", "", "area", "my khe", "ми кхе", "май кхе"),
    ("my_an", "Mỹ An", "", "area", "my an"),
    ("an_thuong", "An Thượng", "", "area", "an thuong", "ан тхыонг"),
    ("phuoc_my", "Phước Mỹ", "", "area", "phuoc my"),
    ("hoa_xuan", "Hòa Xuân", "", "area", "hoa xuan"),
    ("hoa_khanh", "Hòa Khánh", "", "area", "hoa khanh"),
    ("khue_my", "Khuê Mỹ", "", "area", "khue my"),
    ("hoa_minh", "Hòa Minh", "", "area", "hoa minh"),
    ("hoa_hai", "Hòa Hải", "", "area", "hoa hai"),
    ("an_hai", "An Hải", "", "area", "an hai"),
    ("panoma", "Panoma", "", "complex", "panoma"),
    ("the_ponte", "The Ponte", "", "complex", "the ponte"),
    ("the_filmore", "The Filmore", "", "complex", "the filmore"),
    ("sam_towers", "Sam Towers", "", "complex", "sam towers"),
    ("nam_viet_a", "Nam Việt Á", "", "complex", "nam viet a"),
    ("fpt_city", "FPT City", "", "complex", "fpt city"),
    ("altara", "Altara Suites", "", "complex", "altara"),
    ("monarchy", "Monarchy", "", "complex", "monarchy", "monachy"),
    ("blooming_tower", "Blooming Tower", "", "complex", "blooming tower"),
)  # fmt: skip
