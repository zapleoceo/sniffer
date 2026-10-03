"""Отчёт прохода догона: что просмотрели, что изменили, что пропустили и почему.

Отчёт читает человек перед боевым прогоном и решает по нему, запускать ли, —
поэтому он считает исходы в разрезе (категория, сторона сделки) и показывает
несколько id редких исходов, чтобы их можно было открыть глазами. Ни текстов,
ни заголовков, ни контактов в нём нет: только числа, названия исходов и id
карточек — отчёт уходит в `docker logs` и в историю терминала.

Ключи счётчиков:

* `seen` — просмотрено; `written` — записано (в dry-run — `would_write`: записали
  бы);
* `<вывод>.<исход>` — то, что вывод решил про карточку: `price.filled`,
  `price.replaced`… Отчёт про них ничего не знает и считает любые, так что новый
  вывод попадает в него без правки этого файла;
* `after_verdict.<исход>` — из записанных `price.filled`, `price.replaced` и
  `price.erased` те, что объясняются сменой стороны сделки или категории после
  вердикта модели: чтение текста под прежней парой воспроизводит то, что лежало
  в карточке, а под итоговой даёт иное (`worker/enrich_origin.py`). Это не
  отдельный исход (сумма `price.*` с пропусками по-прежнему равна просмотренному),
  а пометка поверх него; `after_verdict.unknown` — проверка не удалась;
* `skip.<причина>` — карточка пропущена: `no_text` (сырья нет), `derive_error`
  (вывод не справился), `changed_since_read` (строка изменилась с момента чтения),
  `write_error` (база отказала на этой строке), `batch_failed` (не записалась
  вся пачка).

Исход считается ТОЛЬКО у записанной карточки (в dry-run — у той, что записали
бы) и у той, которой писать нечего: у пропущенной он ложь, «заполнено» не
должно включать строки, где запись не состоялась.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from sniffer.domain.records import Listing
from sniffer.pipeline import enrich_price as price

SEEN = "seen"
WRITTEN = "written"
WOULD_WRITE = "would_write"
SKIP = "skip."

SAMPLES_PER_KEY = 10
# Исход, случившийся не чаще этого, разбирают глазами — для него показываем id.
# Массовые (`filled` — тысячи) глазами не разбирают, и список id был бы шумом.
REVIEW_UP_TO = 100

AFTER_VERDICT = "after_verdict."
# Стираемые цены печатаются все: это и есть то, что владелец смотрит глазами до
# боевого прогона. Больше предела — первые и счёт остатка, отчёт не должен
# превращаться в стену.
ERASED_LISTED = 200

END = "end"
LIMIT = "limit"

_NOT_COUNTED = frozenset({SEEN, WRITTEN, WOULD_WRITE})


@dataclass
class EnrichReport:
    dry_run: bool = False
    last_id: int = 0
    # Чем кончилось: `end` — дошли до конца, `limit` — упёрлись в --limit,
    # пусто — проход прерван (сбой или Ctrl+C), и продолжать надо с `last_id`.
    stop_reason: str = ""
    scopes: dict[tuple[str, str], Counter[str]] = field(default_factory=dict)
    samples: dict[str, list[int]] = field(default_factory=dict)
    # id -> старая сумма стёртой цены (строкой, как она ляжет в `price_erased`).
    erased: dict[int, str] = field(default_factory=dict)

    def _scope(self, listing: Listing) -> Counter[str]:
        return self.scopes.setdefault((listing.category, listing.deal_type), Counter())

    def _note(self, listing: Listing, key: str) -> None:
        ids = self.samples.setdefault(key, [])
        if len(ids) < SAMPLES_PER_KEY and listing.id is not None:
            ids.append(listing.id)

    def seen(self, listing: Listing) -> None:
        self._scope(listing)[SEEN] += 1

    def outcomes(self, listing: Listing, names: Iterable[str]) -> None:
        for name in names:
            self._scope(listing)[name] += 1
            self._note(listing, name)

    def erased_price(self, listing: Listing) -> None:
        """Запомнить, какая именно цена стирается: id и сумма, ничего больше."""
        if listing.id is not None and listing.price_amount is not None:
            self.erased[listing.id] = str(listing.price_amount)

    def skip(self, listing: Listing, reason: str) -> None:
        key = SKIP + reason
        self._scope(listing)[key] += 1
        self._note(listing, key)

    def wrote(self, listing: Listing) -> None:
        self._scope(listing)[WOULD_WRITE if self.dry_run else WRITTEN] += 1

    def totals(self) -> Counter[str]:
        total: Counter[str] = Counter()
        for counter in self.scopes.values():
            total.update(counter)
        return total

    def skipped(self) -> int:
        return sum(n for key, n in self.totals().items() if key.startswith(SKIP))

    def fields(self) -> dict[str, object]:
        """Поля структурного лога: числа и id, больше ничего."""
        return {
            "dry_run": self.dry_run,
            "stop_reason": self.stop_reason,
            "last_id": self.last_id,
            "skipped": self.skipped(),
            "totals": dict(self.totals()),
            "scopes": {f"{c}/{d}": dict(n) for (c, d), n in sorted(self.scopes.items())},
            "samples": self.samples,
            "erased_prices": dict(list(self.erased.items())[:ERASED_LISTED]),
        }

    def render(self) -> str:
        """Текст для человека: итог, исходы целиком и по каждой категории."""
        total = self.totals()
        done = total[WOULD_WRITE] if self.dry_run else total[WRITTEN]
        if self.dry_run:
            head = f"Проход догона — dry-run: в базу НИЧЕГО не записано (записали бы: {done})."
        else:
            head = f"Проход догона — боевой прогон, записано в базу: {done}."
        summary = (
            f"Итог: просмотрено {total[SEEN]} · заполнено {total[price.FILLED]} · "
            f"заменено {total[price.REPLACED]} · стёрто {total[price.ERASED]} · "
            f"расхождений {total[price.DISAGREED]} · потеряно {total[price.LOST]} · "
            f"пропущено {self.skipped()}"
        )
        verdict = (
            "Из них объясняются сменой стороны или категории после вердикта: "
            f"заполнено {total[AFTER_VERDICT + 'filled']} · "
            f"заменено {total[AFTER_VERDICT + 'replaced']} · "
            f"стёрто {total[AFTER_VERDICT + 'erased']}"
        )
        lines = [head, summary, verdict, self._stop_line(), "", "По исходам:"]
        lines += self._counts(total, show_ids=True)
        for (category, deal), counter in sorted(self.scopes.items()):
            lines += ["", f"{category} / {deal}: просмотрено {counter[SEEN]}"]
            lines += self._counts(counter, show_ids=False)
        lines += self._erased_lines()
        return "\n".join(lines)

    def _erased_lines(self) -> list[str]:
        if not self.erased:
            return []
        title = (
            "Цены, которые стёрли бы (id → прежняя сумма):"
            if self.dry_run
            else "Стёртые цены (id → прежняя сумма):"
        )
        lines = ["", title]
        shown = sorted(self.erased.items())[:ERASED_LISTED]
        lines += [f"  {listing_id} → {amount}" for listing_id, amount in shown]
        if len(self.erased) > len(shown):
            lines.append(f"  … и ещё {len(self.erased) - len(shown)}")
        return lines

    def _stop_line(self) -> str:
        if self.stop_reason == END:
            return f"Дошли до конца; последний id {self.last_id}."
        if self.stop_reason == LIMIT:
            return f"Остановились по --limit; последний id {self.last_id}."
        return f"ПРОХОД НЕ ДОШЁЛ ДО КОНЦА: продолжить можно с --since-id {self.last_id}."

    def _counts(self, counter: Counter[str], *, show_ids: bool) -> list[str]:
        lines = []
        for key in sorted(k for k in counter if k not in _NOT_COUNTED):
            line = f"  {key:<30}{counter[key]:>7}"
            if show_ids and counter[key] <= REVIEW_UP_TO and self.samples.get(key):
                line += "   id: " + ", ".join(str(i) for i in self.samples[key])
            lines.append(line)
        return lines
