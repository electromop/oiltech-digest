-- JPT читался одной страницей /latest-news (14 карточек), при том что у издания 40+
-- тематических разделов. Замер 13.09: 11 разделов дают 99 уникальных статей, из них
-- 66 у нас НЕТ. Заказчик 24.08: «обратите внимание на источник JPT, для нефтянки это
-- маст хэв, номер 1 в мире, а оттуда мало что подтягивается».
-- Селекторы те же, что у основного JPT (source_overrides: Journal of Petroleum Technology).
INSERT INTO sources (name, source_type, url, enabled, parse_strategy, listing_url,
                     listing_selector, article_link_selector, article_date_selector,
                     category, priority)
VALUES
 ('JPT — R&D и инновации','Journal','https://jpt.spe.org',TRUE,'playwright','https://jpt.spe.org/topic/r-d-innovation','.PromoB, .PromoA','.PromoB-title a, .PromoA-title a','.PromoB-by-line, .PromoA-by-line','международные',3),
 ('JPT — Роботизация','Journal','https://jpt.spe.org',TRUE,'playwright','https://jpt.spe.org/topic/robotics-unmanned-systems','.PromoB, .PromoA','.PromoB-title a, .PromoA-title a','.PromoB-by-line, .PromoA-by-line','международные',3),
 ('JPT — Бурение','Journal','https://jpt.spe.org',TRUE,'playwright','https://jpt.spe.org/topic/drilling','.PromoB, .PromoA','.PromoB-title a, .PromoA-title a','.PromoB-by-line, .PromoA-by-line','международные',3),
 ('JPT — Заканчивание','Journal','https://jpt.spe.org',TRUE,'playwright','https://jpt.spe.org/topic/completions','.PromoB, .PromoA','.PromoB-title a, .PromoA-title a','.PromoB-by-line, .PromoA-by-line','международные',3),
 ('JPT — Внутрискважинные работы','Journal','https://jpt.spe.org',TRUE,'playwright','https://jpt.spe.org/topic/well-intervention','.PromoB, .PromoA','.PromoB-title a, .PromoA-title a','.PromoB-by-line, .PromoA-by-line','международные',3),
 ('JPT — Инспекция и ТОиР','Journal','https://jpt.spe.org',TRUE,'playwright','https://jpt.spe.org/topic/inspection-maintenance','.PromoB, .PromoA','.PromoB-title a, .PromoA-title a','.PromoB-by-line, .PromoA-by-line','международные',3),
 ('JPT — Промышленная безопасность','Journal','https://jpt.spe.org',TRUE,'playwright','https://jpt.spe.org/topic/safety','.PromoB, .PromoA','.PromoB-title a, .PromoA-title a','.PromoB-by-line, .PromoA-by-line','международные',3),
 ('JPT — Водоподготовка','Journal','https://jpt.spe.org',TRUE,'playwright','https://jpt.spe.org/topic/water-management','.PromoB, .PromoA','.PromoB-title a, .PromoA-title a','.PromoB-by-line, .PromoA-by-line','международные',3)
ON CONFLICT (name, source_type) DO UPDATE SET
  listing_url = EXCLUDED.listing_url,
  listing_selector = EXCLUDED.listing_selector,
  article_link_selector = EXCLUDED.article_link_selector,
  article_date_selector = EXCLUDED.article_date_selector,
  parse_strategy = EXCLUDED.parse_strategy,
  updated_at = now();

SELECT id, left(name,34) AS name, parse_strategy, left(listing_url,48) AS listing
FROM sources WHERE name LIKE 'JPT — %' ORDER BY id;
