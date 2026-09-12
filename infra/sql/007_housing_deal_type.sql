-- Жильё в Нячанге снимают: объявление о квартире, комнате или доме без глагола
-- сделки — предложение сдать. Конвейер до 12.09.2026 записывал его продажей
-- (`sell`, период `once`); замер по 28 дням: из 3714 карточек жилья со
-- стороной `sell` 2707 по тексту про аренду и лишь 53 про продажу. Правило
-- теперь живёт в `domain.passport.default_deal_type`; здесь — история.
--
-- Идемпотентно: карточки, названные продажей словом, не трогаются, а уже
-- перелицованные второй раз под условие не попадут.
UPDATE listings
   SET deal_type = 'rent_out',
       price_period = CASE WHEN price_amount IS NULL THEN price_period ELSE 'month' END
 WHERE category IN ('apartment', 'room', 'house')
   AND deal_type = 'sell'
   AND source = 'telegram_archive'
   AND summary !~* '(прода(м|ю|ется|ётся|жа)|for sale|\mbán\M)';
