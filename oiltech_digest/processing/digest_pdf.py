"""PDF выпуска в формате эксперта (#161, спека #160): обложка на весь первый лист и листы карточек, как первые листы
итоговой редакции эксперта, без расширенных статей. Тот же PDF и для предпросмотра на экране «Дайджест», и для
выгрузки; письмо и DOCX собираются по-прежнему (processing.digest).

Вид: макет, согласованный владельцем 02.11 (вне репозитория). Набор как у образца эксперта: шрифт бренда (файлы с
сервера, brand_fonts) обычный, жирный и узкий жирный, его кегли, текст по ширине, без переносов слов и без кернинга. Колонтитул со второго
листа и номер «N / M» на каждом дают поля листа (@page): колонтитул Chromium на первом листе не скрыть. Обложка
фиксированной высоты, ровно в лист: если она хоть немного выше листа, Chromium ужимает весь первый лист, и баннер
перестаёт доходить до края (на макете 198 мм вместо 210). Не влезает текст: обрезается вступление, а «Главное» и
блок платформы остаются целиком. Переходов внутри PDF, «Стр. N», содержания, служебных строк и серого фона нет.
Закладки на два раздела Chromium строит по заголовкам h1; название и тему документа пишет pypdf.
"""

from __future__ import annotations

import base64
import io
import logging
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from oiltech_digest import brand_fonts
from oiltech_digest.db.issue_edits_store import PICTURE_PLACEHOLDER
from oiltech_digest.processing.digest_materials import MAX_IMAGE_RATIO, MIN_IMAGE_WIDTH, THEMES

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
# Заглушки картинки карточки: по одной на каждую из 13 тем (тема равна корневому тегу), рисунки собственные.
PLACEHOLDERS_DIR = ASSETS_DIR / "placeholders"
# Подпись картинки карточки в размеченном PDF: своя или рисунок темы (тема строчными, как в «Главном»).
OWN_PICTURE_ALT = "Картинка публикации"
_PLACEHOLDER_ALTS = {theme.placeholder: f"Рисунок темы: {theme.phrase}" for theme in THEMES}
# Файлы брендинга, которые выпуск берёт с сервера (баннер, логотип полосы, QR): только картинки.
_BRANDING_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                         ".svg": "image/svg+xml"}

# Сколько PDF ждёт картинки карточек. Их Chromium грузит сам, с сайтов-источников; хост, который принял соединение и
# молчит, держал печать до потолка Playwright в 30 с, и выгрузка падала на каждой попытке (замер 29.09). Не дождались,
# сайт отказал (403/404) или картинка негодна (уже 400 px или вытянута сильнее 3:1: логотип, полоска): заглушка темы
# карточки. Возвращает, сколько картинок заменено.
IMAGES_WAIT_MS = 15_000
_SWAP_IMAGES_JS = """
async ([minWidth, maxRatio]) => {
  window.stop();
  const swapped = [];
  document.querySelectorAll("img[data-placeholder]").forEach((img) => {
    const width = img.naturalWidth, height = img.naturalHeight;
    const fit = img.complete && width >= minWidth && height > 0
      && Math.max(width, height) / Math.min(width, height) <= maxRatio;
    if (fit) return;
    img.src = img.dataset.placeholder;
    img.alt = img.dataset.placeholderAlt;
    swapped.push(img.decode().catch(() => undefined));
  });
  await Promise.all(swapped);
  return swapped.length;
}
"""


def render(content: dict, *, files_dir: Path | None) -> bytes:
    """PDF выпуска из содержания сборщика (processing.digest.build_digest_content). `files_dir`: каталог брендинга на
    сервере: там лежат файлы баннера, логотипа и QR, которые называет брендинг."""
    document = content.get("document") or {}
    pdf = _print(html(content, files_dir=files_dir))
    return _with_metadata(pdf, title=document.get("title") or "", subject=document.get("subject") or "")


def html(content: dict, *, files_dir: Path | None) -> str:
    document = content.get("document") or {}
    body = _cover_html(content, files_dir) + "".join(
        _section_html(section) for section in content.get("sections") or [] if section.get("materials")
    )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{_e(document.get('title'))}</title><style>{font_faces()}{_css(document)}</style></head>"
        f"<body>{body}</body></html>"
    )


def font_faces() -> str:
    """@font-face шрифта бренда в base64: рендер не зависит от шрифтов ОС. Файлы с сервера (brand_fonts); нет файла,
    нет и правила, и печать берёт запасной шрифт из списка семейства в _CSS."""
    faces = []
    for name, (family, weight) in brand_fonts.FILES.items():
        path = brand_fonts.font_path(name)
        if path is not None:
            faces.append(
                f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{weight};"
                f"src:url({_data_uri(path.read_bytes(), 'font/ttf')}) format('truetype');}}"
            )
    return "".join(faces)


# --- обложка --------------------------------------------------------------------------------------------------------

def _cover_html(content: dict, files_dir: Path | None) -> str:
    """Обложка из брендинга на сервере. Баннер, файл в каталоге брендинга, как логотип и QR: в репозитории только
    баннер письма, у него вкладка «НОВОСТИ» нарисована на самой картинке, а при пустом брендинге вкладки быть не
    должно. Нет файла: нет баннера."""
    branding = (content.get("branding") or {}).get("cover") or {}
    cover = content.get("cover") or {}
    banner = _branding_image(files_dir, branding.get("banner_file"))
    # Вкладка стоит на баннере: без баннера ей не на чем стоять, и она легла бы на вступление.
    hero = f'<div class="hero"><img class="banner" src="{banner}" alt="">{_tab_html(branding)}</div>' if banner else ""
    return (
        '<section class="cover">'
        f"{_masthead_html(branding, files_dir)}"
        f"{hero}"
        '<div class="cover-body">'
        f"{_paragraph('intro', branding.get('intro'))}"
        f"{_highlights_html(cover)}"
        f"{_platform_html(branding, files_dir)}"
        "</div></section>"
    )


def _masthead_html(branding: dict, files_dir: Path | None) -> str:
    """Полоса над баннером: логотип слева, подпись справа. Нет ни того, ни другого: полосы нет."""
    logo = _branding_image(files_dir, branding.get("logo_file"))
    caption = _text(branding.get("masthead_caption"))
    if not logo and not caption:
        return ""
    logo_html = f'<img class="logo" src="{logo}" alt="">' if logo else "<span></span>"
    caption_html = f'<div class="unit">{_e(caption)}</div>' if caption else ""
    return f'<div class="masthead">{logo_html}{caption_html}</div>'


def _tab_html(branding: dict) -> str:
    tab = _text(branding.get("tab_text"))
    return f'<div class="tab">{_e(tab)}</div>' if tab else ""


def _highlights_html(cover: dict) -> str:
    rows = "".join(
        f'<div class="hl"><div class="hl-label">{_e(row["label"])}</div>'
        f'<div class="hl-text">{_e(row["text"])}</div></div>'
        for row in cover.get("highlights") or [] if row.get("text")
    )
    if not rows:
        return ""
    return f'<div class="hl-block"><div class="hl-title">{_e(cover.get("highlights_title"))}</div>{rows}</div>'


def _platform_html(branding: dict, files_dir: Path | None) -> str:
    """Блок платформы: ссылка, Telegram с QR, адрес для обратной связи. Пустое поле: нет и строки; пусто всё: нет
    блока."""
    rows = []
    platform = _safe_href(branding.get("platform_url"))
    if platform:
        rows.append(("Платформа", "Эти и другие сигналы доступны бесплатно на платформе "
                     f'<a href="{_e(platform)}">{_e(_host_text(platform))}</a>'))
    telegram = _safe_href(branding.get("telegram_url"))
    if telegram:
        rows.append(("Telegram", "В канале можно предлагать свои новости, сигналы и материалы для изучения. "
                     f'<a href="{_e(telegram)}">Перейти в Telegram →</a>'))
    email = _text(branding.get("feedback_email"))
    if email:
        rows.append(("Обратная связь", "Замечания и предложения по дайджесту, темы и материалы для следующих выпусков "
                     f'присылайте на адрес группы: <a href="mailto:{_e(email)}">{_e(email)}</a>'))
    note = _text(branding.get("platform_note"))
    if note:
        rows.append(("", _e(note)))
    if not rows:
        return ""
    title = _text(branding.get("platform_title"))
    title_html = f'<div class="radar-title">{_e(title)}</div>' if title else ""
    rows_html = "".join(f'<div class="rr"><b>{_e(label)}</b><span>{text}</span></div>' for label, text in rows)
    qr = _branding_image(files_dir, branding.get("qr_file"))
    qr_html = ""
    if qr:
        image = f'<img src="{qr}" alt="">Telegram-канал'
        qr_html = f'<a class="qr" href="{_e(telegram)}">{image}</a>' if telegram else f'<div class="qr">{image}</div>'
    return f'<div class="radar"><div class="radar-body">{title_html}{rows_html}</div>{qr_html}</div>'


# --- разделы и карточки ---------------------------------------------------------------------------------------------

def _section_html(section: dict) -> str:
    cards = "".join(_card_html(material, section.get("criteria_names") or []) for material in section["materials"])
    return (f'<section class="list"><div class="sec-num">{_e(section["number"])}</div>'
            f'<h1>{_e(section["title"])}</h1>{cards}</section>')


def _card_html(material: dict, section_names: list[str]) -> str:
    url = _safe_href(material.get("url"))
    title = _e(material.get("title"))
    title_html = f'<a class="title" href="{_e(url)}">{title}</a>' if url else f'<div class="title">{title}</div>'
    read = f'<a class="read" href="{_e(url)}">Читать материал →</a>' if url else ""
    rubric_line = " / ".join(part for part in (material.get("rubric"), material.get("label")) if part)
    rubric = f'<div class="rub">{_e(rubric_line)}</div>' if rubric_line else ""
    return (
        '<div class="card"><div class="top">'
        f'<div class="side">{_picture_html(material)}{read}</div>'
        f'<div class="txt">{rubric}{title_html}{_paragraph("sum", material.get("summary"))}</div>'
        f"</div>{_hypothesis_html(material)}{_criteria_html(material, section_names)}</div>"
    )


def _hypothesis_html(material: dict) -> str:
    """«Гипотеза для Компании» под сутью, на всю ширину карточки (макет 02.11); нет гипотезы: нет и строки."""
    text = _text(material.get("hypothesis"))
    if not text:
        return ""
    return ('<div class="hyp"><div class="hyp-label">Гипотеза для Компании</div>'
            f'<p class="hyp-text">{_e(text)}</p></div>')


def _picture_html(material: dict) -> str:
    """Своя картинка публикации или заглушка темы. Своя, которая не загрузилась или негодна, при печати меняется на
    заглушку своей темы (_SWAP_IMAGES_JS); заглушку, выбранную человеком (#162), печать рисует и при своей."""
    placeholder = _placeholder_uri(material.get("placeholder"))
    placeholder_alt = _e(_PLACEHOLDER_ALTS.get(material.get("placeholder"), ""))
    chosen_own = material.get("picture") != PICTURE_PLACEHOLDER
    own = material.get("image_url") if material.get("image_own") and chosen_own else ""
    if own:
        return (f'<img class="pic" src="{_e(own)}" data-placeholder="{placeholder}" '
                f'data-placeholder-alt="{placeholder_alt}" alt="{OWN_PICTURE_ALT}">')
    if not placeholder:
        return '<div class="pic"></div>'
    return f'<img class="pic" src="{placeholder}" alt="{placeholder_alt}">'


def _criteria_html(material: dict, section_names: list[str]) -> str:
    """Таблица под карточкой: баллы по критериям и «Связь с нефтесервисом». Без разбивки: «нет оценки по критериям»
    под именами критериев профиля раздела."""
    criteria = material.get("criteria") or []
    names = [row["name"] for row in criteria] or section_names
    # Набора критериев нет (пустой профиль): пустая ячейка над «нет оценки», чтобы связь стояла над связью.
    head = ("".join(f"<th>{_e(name)}</th>" for name in names) or "<th></th>") + "<th>Связь с нефтесервисом</th>"
    if criteria:
        cells = "".join('<td class="muted">не входит</td>' if row.get("excluded") else f"<td>{_e(row['score'])}</td>"
                        for row in criteria)
    else:
        cells = f'<td colspan="{max(len(names), 1)}" class="muted">нет оценки по критериям</td>'
    relevance = _e(material.get("relevance") or "")
    return f'<table class="crit"><tr>{head}</tr><tr>{cells}<td class="rel">{relevance}</td></tr></table>'


# --- печать ---------------------------------------------------------------------------------------------------------

def _print(html_text: str) -> bytes:
    """PDF из HTML в headless Chromium. Playwright с Chromium: серверная зависимость (тяжёлая, тестам и письму не
    нужна), поэтому импорт ленивый, а без него понятная ошибка."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on optional dep
        raise RuntimeError(
            "PDF-экспорт требует Playwright с Chromium. Установите на сервере: "
            "pip install playwright && python -m playwright install --with-deps chromium"
        ) from exc

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.set_content(html_text, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("load", timeout=IMAGES_WAIT_MS)
                except PlaywrightTimeoutError:
                    pass  # недогрузившиеся картинки заменит заглушка ниже
                replaced = page.evaluate(_SWAP_IMAGES_JS, [MIN_IMAGE_WIDTH, MAX_IMAGE_RATIO])
                if replaced:
                    logger.warning("digest_pdf_images_replaced count=%s", replaced)
                return page.pdf(format="A4", print_background=True, prefer_css_page_size=True, outline=True,
                                tagged=True)
            finally:
                browser.close()
    except PlaywrightError as exc:
        # Упал или не запустился сам браузер: ошибка Playwright не RuntimeError, и экран получал 500 (ревью PR #155).
        # Человеку понятный текст, причина в журнале.
        logger.exception("digest_pdf_browser_failed")
        raise RuntimeError("Браузер не смог собрать PDF. Повторите через минуту.") from exc


def _with_metadata(pdf: bytes, *, title: str, subject: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    writer.add_metadata({"/Title": title, "/Subject": subject})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- мелочи ---------------------------------------------------------------------------------------------------------

def _e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _text(value: object) -> str:
    return str(value or "").strip()


def _paragraph(css_class: str, value: object) -> str:
    text = _text(value)
    return f'<p class="{css_class}">{_e(text)}</p>' if text else ""


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _safe_href(url: object) -> str:
    """Ссылка наружу: только абсолютный http(s). Относительную Chromium из PDF выбрасывает (урок выпуска за август),
    а иная схема читателю не нужна."""
    text = _text(url)
    parsed = urlparse(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _host_text(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc + parsed.path).rstrip("/")


def _placeholder_uri(key: object) -> str:
    path = PLACEHOLDERS_DIR / f"{key}.svg"
    return _data_uri(path.read_bytes(), "image/svg+xml") if key and path.is_file() else ""


def _branding_image(files_dir: Path | None, name: object) -> str:
    """Картинка брендинга с сервера по имени файла в каталоге брендинга. Только имя файла (не путь) и только картинка:
    брендинг правит админ, но читать файлы вне своего каталога выпуску незачем. Нет файла: нет и элемента."""
    file_name = _text(name)
    if not file_name or files_dir is None:
        return ""
    mime = _BRANDING_IMAGE_TYPES.get(Path(file_name).suffix.lower())
    if mime is None or Path(file_name).name != file_name or file_name.startswith("."):
        logger.warning("digest_branding_file_refused name=%r", file_name)
        return ""
    path = files_dir / file_name
    if not path.is_file():
        logger.warning("digest_branding_file_missing name=%r", file_name)
        return ""
    return _data_uri(path.read_bytes(), mime)


def _css_string(value: object) -> str:
    """Значение для content: в CSS: строка в кавычках. Название выпуска правит админ брендинга, а строка стоит внутри
    <style>: «<» идёт CSS-экраном (иначе «</style>» закрыл бы стиль, и разметка после него исполнилась бы в
    Chromium сервера), кавычка и обратная косая экранируются, переводы строк: пробелы."""
    text = str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3c ")
    for control in "\r\n\f":
        text = text.replace(control, " ")
    return f'"{text}"'


def _css(document: dict) -> str:
    name = _css_string((document.get("name") or "").upper())
    month = _css_string(document.get("month_label") or "")
    # Месяц первым: название (его правит админ) может содержать «%MONTH%», месяц (его пишет код): нет.
    return _CSS.replace("%MONTH%", month).replace("%NAME%", name)


_CSS = """
@page{size:A4;margin:21mm 16.6mm 15mm 16.6mm;
  @top-left{content:%NAME%;font:700 8.2pt 'Brand Sans',Arial,sans-serif;letter-spacing:.05em;color:#00469d;
    vertical-align:bottom;padding-bottom:2.2mm;margin-bottom:4mm;border-bottom:.6pt solid #d5dfeb;}
  @top-right{content:%MONTH%;font:400 8.2pt 'Brand Sans',Arial,sans-serif;color:#56677e;text-align:right;
    vertical-align:bottom;padding-bottom:2.2mm;margin-bottom:4mm;border-bottom:.6pt solid #d5dfeb;}
  @bottom-right{content:counter(page) " / " counter(pages);font:700 8.2pt 'Brand Sans',Arial,sans-serif;color:#00469d;}
}
@page :first{margin:0 0 15mm 0;@top-left{content:none;border:none;}@top-right{content:none;border:none;}
  @bottom-right{padding-right:16.6mm;color:#56677e;font-weight:400;}}
html{font-family:'Brand Sans',Arial,sans-serif;color:#1b2432;-webkit-print-color-adjust:exact;
  font-kerning:none;font-variant-ligatures:none;hyphens:manual;}
body{margin:0;}
a{color:inherit;text-decoration:none;}
.cover{height:282mm;display:flex;flex-direction:column;overflow:hidden;}
.masthead{flex:0 0 17mm;display:flex;justify-content:space-between;align-items:center;padding:0 12mm;background:#fff;}
.logo{display:block;flex:0 0 auto;height:11.6mm;width:auto;max-width:90mm;}
.unit{font:700 12pt 'Brand Sans',Arial,sans-serif;letter-spacing:.03em;text-transform:uppercase;color:#003da6;}
.hero{position:relative;flex:0 0 auto;}
.banner{display:block;width:210mm;height:auto;max-height:120mm;object-fit:cover;}
.tab{position:absolute;left:12mm;top:0;min-width:41mm;height:12.6mm;padding:0 4mm;box-sizing:border-box;
  background:#ff5600;border-radius:0 0 3mm 3mm;display:flex;align-items:center;justify-content:center;
  font:700 14pt 'Brand Sans',Arial,sans-serif;letter-spacing:.04em;text-transform:uppercase;color:#fff;}
.cover-body{flex:1;min-height:0;display:flex;flex-direction:column;justify-content:space-between;
  padding:5mm 16.6mm 3mm 16.6mm;}
.intro{font-size:11.8pt;line-height:1.38;text-align:justify;margin:0;min-height:0;overflow:hidden;}
.hl-title{font:700 12.5pt 'Brand Sans',Arial,sans-serif;letter-spacing:.03em;text-transform:uppercase;color:#00469d;margin:0 0 1.5mm;}
.hl{display:flex;gap:6mm;padding:2.2mm 0;border-bottom:.8pt solid #d5dfeb;}
.hl-label{flex:0 0 42mm;font:700 13.5pt/1.3 'Brand Sans',Arial,sans-serif;color:#00469d;}
.hl-text{flex:1;font-size:12.5pt;line-height:1.38;text-align:justify;}
.radar{display:flex;gap:6mm;align-items:center;padding:3.6mm 5.5mm 2.4mm;border:.8pt solid #dce2ea;border-radius:9px;}
.radar-body{flex:1;}
.radar-title{font:700 12pt 'Brand Sans',Arial,sans-serif;color:#00469d;margin:0 0 2.4mm;}
.rr{display:flex;gap:4mm;font-size:10.8pt;line-height:1.32;margin:0 0 1.6mm;}
.rr b{flex:0 0 29mm;}
.rr a{color:#00469d;font-weight:700;white-space:nowrap;}
.qr{flex:0 0 auto;text-align:center;font-size:10pt;color:#1b2432;}
.qr img{display:block;width:31mm;margin:0 auto 1.6mm;}
.list{break-before:page;}
.sec-num{font:700 8.5pt 'Brand Sans',Arial,sans-serif;color:#ff5600;margin:0 0 1mm;}
h1{font:700 29pt/1.1 'Brand Sans Condensed','Arial Narrow',Arial,sans-serif;color:#00469d;margin:0 0 4mm;}
.card{border:.8pt solid #c5d7ec;border-radius:7px;padding:2.6mm 4mm 2mm;margin:0 0 2.4mm;break-inside:avoid;}
.top{display:flex;gap:4mm;align-items:flex-start;margin:0 0 1.4mm;}
.side{flex:0 0 44mm;}
.side .read{display:block;margin:1.6mm 0 0;}
.pic{width:44mm;height:33mm;object-fit:cover;border-radius:4px;display:block;}
.txt{flex:1;min-width:0;}
.rub{font:700 8.5pt 'Brand Sans',Arial,sans-serif;color:#ff5600;margin:0 0 1.2mm;}
.title{display:block;font:700 16pt/1.1 'Brand Sans Condensed','Arial Narrow',Arial,sans-serif;color:#00469d;margin:0 0 1.3mm;}
.sum{font-size:10.5pt;line-height:1.29;text-align:justify;margin:0;}
.hyp{margin:0 0 1.6mm;}
.hyp-label{font:700 8.5pt 'Brand Sans',Arial,sans-serif;color:#00469d;margin:0 0 .5mm;}
.hyp-text{font:700 10.5pt/1.29 'Brand Sans',Arial,sans-serif;color:#00469d;margin:0;}
.read{font:700 8.3pt 'Brand Sans',Arial,sans-serif;color:#ff5600;}
.crit{width:100%;border-collapse:collapse;table-layout:fixed;margin:1.4mm 0 0;}
.crit th{font:400 6.4pt/1.12 'Brand Sans',Arial,sans-serif;color:#56677e;text-align:left;vertical-align:bottom;padding:1mm 1.6mm .5mm;
  border-top:.6pt solid #d5dfeb;border-left:.6pt solid #d5dfeb;}
.crit td{font:700 9.6pt 'Brand Sans',Arial,sans-serif;color:#1b2432;padding:.2mm 1.6mm .2mm;border-left:.6pt solid #d5dfeb;}
.crit th:first-child,.crit td:first-child{border-left:none;padding-left:0;}
.crit td.muted{font-weight:400;font-size:8.5pt;color:#56677e;}
.crit td.rel{color:#00469d;}
"""
