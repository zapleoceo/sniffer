"""Нячанг: районы, ЖК и улицы-ориентиры — данные для `facts_place`.

Строка: (канон, имя, зона, вид, *написания). Канон — ASCII-слаг, он и лежит в
`listings.district`. Написания — без диакритики и в нижнем регистре (текст сворачивается
тем же `facts_text.fold`): по-вьетнамски, по-русски (у русских «Phước Hải» пишут и
«Фуок Хай», и «Фыок Хай», и «Фыокхай»), как написано в постах.

Названия даны по обе стороны реформы 2025 года: прежние варды (Vĩnh Hải, Phước Hải…) и
новые (Bắc / Nam / Tây Nha Trang). Карта «старый вард → новый» сюда не вносилась — её
источник официальный указ, а не частота в постах, и выдумывать её нельзя.

Зона (север/центр/юг/запад) — так, как её называют русскоязычные агентства («Север
Нячанга»), и проставлена ТОЛЬКО там, где замер по 16 600 постам жилья (03.10.2026)
дал ≥ 10 совпадений и ≥ 80% согласия: «Oceanus» назван севером в 1185 из 1190 постов, а
Phước Hải — югом лишь в 66%, остальное запад и центр, поэтому зоны у него нет и она
берётся из слов самого поста. Где доказательств меньше, зона пуста, а не выдумана.

Вид: `area` — вард, район, остров; `complex` — жилой комплекс; `street` — улица. Улицы
повторяются в городах, и при споре городов теряют силу первыми (`facts_place`).
"""

from __future__ import annotations

NHA_TRANG: tuple[tuple[str, ...], ...] = (
    ("vinh_hai", "Vĩnh Hải", "north", "area", "vinh hai", "винь хай", "виньхай", "винь хаи"),
    (
        "vinh_phuoc", "Vĩnh Phước", "north", "area",
        "vinh phuoc", "винь фыок", "винь фуок", "виньфыок", "виньфуок",
    ),
    ("vinh_hoa", "Vĩnh Hòa", "north", "area", "vinh hoa", "винь хоа", "виньхоа"),
    ("vinh_tho", "Vĩnh Thọ", "", "area", "vinh tho", "винь тхо", "виньтхо"),
    ("vinh_nguyen", "Vĩnh Nguyên", "south", "area", "vinh nguyen", "винь нгуен", "виньнгуен"),
    (
        "vinh_truong", "Vĩnh Trường", "south", "area",
        "vinh truong", "винь чыонг", "винь чуонг", "виньчыонг",
    ),
    ("vinh_thai", "Vĩnh Thái", "", "area", "vinh thai", "винь тхай", "виньтхай"),
    ("vinh_ngoc", "Vĩnh Ngọc", "", "area", "vinh ngoc", "винь нгок"),
    ("vinh_luong", "Vĩnh Lương", "", "area", "vinh luong"),
    ("vinh_phuong", "Vĩnh Phương", "", "area", "vinh phuong"),
    ("vinh_thanh", "Vĩnh Thạnh", "", "area", "vinh thanh"),
    ("vinh_trung", "Vĩnh Trung", "", "area", "vinh trung"),
    ("vinh_hiep", "Vĩnh Hiệp", "", "area", "vinh hiep"),
    (
        "vinh_diem_trung", "Vĩnh Điềm Trung", "west", "area",
        "vinh diem trung", "винь дьем чунг", "винь зьем чунг", "винь дием чунг",
        "винь зием чунг",
    ),
    ("ngoc_hiep", "Ngọc Hiệp", "", "area", "ngoc hiep", "нгок хиеп"),
    ("van_thanh", "Vạn Thạnh", "", "area", "van thanh", "ван тхань", "вантхань"),
    ("van_thang", "Vạn Thắng", "", "area", "van thang"),
    ("phuong_sai", "Phương Sài", "", "area", "phuong sai", "фыонг сай", "фуонг сай"),
    ("phuong_son", "Phương Sơn", "west", "area", "phuong son"),
    ("phuoc_tien", "Phước Tiến", "", "area", "phuoc tien"),
    ("phuoc_tan", "Phước Tân", "", "area", "phuoc tan"),
    (
        "phuoc_hoa", "Phước Hòa", "center", "area",
        "phuoc hoa", "фуок хоа", "фыок хоа", "фуокхоа", "фыокхоа",
    ),
    (
        "phuoc_long", "Phước Long", "south", "area",
        "phuoc long", "фуок лонг", "фыок лонг", "фуоклонг", "фыоклонг",
    ),
    (
        "phuoc_hai", "Phước Hải", "", "area",
        "phuoc hai", "фуок хай", "фыок хай", "фуокхай", "фыокхай",
    ),
    ("phuoc_dong", "Phước Đồng", "", "area", "phuoc dong"),
    ("loc_tho", "Lộc Thọ", "center", "area", "loc tho", "лок тхо", "локтхо"),
    ("tan_lap", "Tân Lập", "center", "area", "tan lap", "con tan lap", "тан лап", "кон тан лап"),
    ("xuong_huan", "Xương Huân", "", "area", "xuong huan"),
    ("hon_xen", "Hòn Xện", "north", "area", "hon xen", "hon sen", "хон сен", "хон шен", "хонсен"),
    ("hon_chong", "Hòn Chồng", "north", "area", "hon chong", "хон чонг"),
    ("hon_nghe", "Hòn Nghê", "", "area", "hon nghe"),
    ("ba_lang", "Ba Làng", "north", "area", "ba lang"),
    ("bai_tien", "Bãi Tiên", "", "area", "bai tien"),
    ("bac_son", "Bắc Sơn", "north", "area", "bac son", "бак шон"),
    ("an_vien", "An Viên", "south", "area", "an vien", "ан вьен", "анвьен"),
    ("an_binh_tan", "An Bình Tân", "south", "area", "an binh tan", "ан бинь тан"),
    (
        "my_gia", "Mỹ Gia", "west", "area",
        "my gia", "mai gia", "ми гиа", "мигия", "май гиа", "ми зя",
    ),
    ("ha_quang", "Hà Quang", "", "area", "ha quang", "ха куанг", "хакуанг"),
    (
        "bac_nha_trang", "Bắc Nha Trang", "north", "area",
        "bac nha trang", "бак нячанг",
    ),
    ("nam_nha_trang", "Nam Nha Trang", "south", "area", "nam nha trang", "нам нячанг"),
    ("tay_nha_trang", "Tây Nha Trang", "west", "area", "tay nha trang", "тай нячанг"),
    ("oceanus", "Oceanus", "north", "complex", "oceanus", "muong thanh oceanus", "океанус"),
    (
        "muong_thanh_vien_trieu", "Mường Thanh Viễn Triều", "north", "complex",
        "muong thanh vien trieu", "vien trieu",
    ),
    (
        "muong_thanh_04", "Mường Thanh 04", "center", "complex",
        "muong thanh 04", "muong thanh 60 tran phu", "mt04",
    ),
    ("muong_thanh", "Mường Thanh", "", "complex", "muong thanh", "муонг тхань", "мыонг тхань"),
    ("gold_coast", "Gold Coast", "center", "complex", "gold coast"),
    ("scenia_bay", "Scenia Bay", "north", "complex", "scenia bay", "sceniabay"),
    ("panorama", "Panorama", "center", "complex", "panorama nha trang", "panorama"),
    ("napoleon", "Napoleon Castle", "north", "complex", "napoleon castle", "napoleon"),
    ("marina_suites", "Marina Suites", "center", "complex", "marina suites"),
    ("virgo", "Virgo", "center", "complex", "virgo"),
    ("paramount", "Paramount", "north", "complex", "paramount"),
    ("hud_building", "HUD Building", "center", "complex", "hud building"),
    (
        "acc_vuon_xoai", "ACC Vườn Xoài", "center", "complex",
        "acc vuon xoai", "vuon xoai", "вуон соай", "вуон соаи", "выон соай",
    ),
    ("hoang_quan", "Hoàng Quân", "north", "complex", "hoang quan"),
    ("capella", "The Capella", "west", "complex", "the capella", "capella"),
    ("tui_blue", "Tui Blue", "center", "complex", "tui blue"),
    ("charmora", "Charmora City", "", "complex", "charmora"),
    ("mango_garden", "Mango Garden", "", "complex", "mango garden"),
    ("tran_phu", "Trần Phú", "", "street", "tran phu", "чан фу", "bo ke tran phu"),
    ("pham_van_dong", "Phạm Văn Đồng", "north", "street", "pham van dong", "фам ван донг"),
    (
        "hung_vuong", "Hùng Vương", "center", "street",
        "hung vuong", "хунг выонг", "хунг вуонг", "хун вуонг",
    ),
    (
        "nguyen_thien_thuat", "Nguyễn Thiện Thuật", "center", "street",
        "nguyen thien thuat", "нгуен тхиен тхуат",
    ),
    ("nguyen_thi_minh_khai", "Nguyễn Thị Minh Khai", "center", "street", "nguyen thi minh khai"),
    (
        "tran_quy_cap", "Trần Quý Cáp", "center", "street",
        "tran quy cap", "чан куй кап", "чан куи кап",
    ),
    ("le_dai_hanh", "Lê Đại Hành", "center", "street", "le dai hanh", "ле дай хань"),
    (
        "phan_chu_trinh", "Phan Chu Trinh", "center", "street",
        "phan chu trinh", "фан чу чинь", "фантьучинь",
    ),
    ("pham_ngoc_thach", "Phạm Ngọc Thạch", "north", "street", "pham ngoc thach", "фам нгок тхать"),
    ("to_hien_thanh", "Tô Hiến Thành", "center", "street", "to hien thanh", "то хиен тхань"),
)  # fmt: skip
