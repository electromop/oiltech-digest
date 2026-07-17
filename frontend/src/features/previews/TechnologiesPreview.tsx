import { useState } from "react";
import type { ReactNode } from "react";
import styles from "./TechnologiesPreview.module.css";

/**
 * СТАТИЧЕСКИЙ ПРОТОТИП экрана «Технологии».
 * Все числа и тексты — вымышленные литералы. Ни одного запроса к API.
 * Интерактив — только локальный UI-стейт (выбранная карточка, вкладки, вид списка).
 */

/* ------------------------------ icons ------------------------------ */

type IconProps = { size?: number; className?: string };

function Svg({ size = 16, className, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

const BookmarkIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 4h12v16l-6-4-6 4V4z" />
  </Svg>
);

const SearchIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </Svg>
);

const FilterIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 5h18l-7 8v6l-4 2v-8L3 5z" />
  </Svg>
);

const StarIcon = ({ size = 14, className }: IconProps) => (
  <svg
    className={className}
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="currentColor"
    aria-hidden="true"
    focusable="false"
  >
    <path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.7l5.9-.9L12 3.5z" />
  </svg>
);

const CheckIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 14}>
    <path d="m20 6-11 11-5-5" />
  </Svg>
);

const DotsIcon = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="5" r="1.2" />
    <circle cx="12" cy="12" r="1.2" />
    <circle cx="12" cy="19" r="1.2" />
  </Svg>
);

const CloseIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 6l12 12M18 6 6 18" />
  </Svg>
);

const ArrowLeftIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 14}>
    <path d="M19 12H5m0 0 6-6m-6 6 6 6" />
  </Svg>
);

const ChevronDownIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 14}>
    <path d="m6 9 6 6 6-6" />
  </Svg>
);

const ListViewIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 14}>
    <path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" />
  </Svg>
);

const GridViewIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 14}>
    <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
    <rect x="13.5" y="3.5" width="7" height="7" rx="1.5" />
    <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
    <rect x="13.5" y="13.5" width="7" height="7" rx="1.5" />
  </Svg>
);

const BoltIcon = (p: IconProps) => (
  <Svg {...p} size={p.size ?? 22}>
    <path d="M13 2 4.5 13.5H11l-1 8.5 8.5-11.5H12l1-8.5z" />
  </Svg>
);

/* ------------------------------ data ------------------------------ */

type Potential = "Высокий" | "Средний";

type Driver = { title: string; value: string };
type Developer = { name: string; initials: string; country: string; role: string };

type Technology = {
  id: string;
  title: string;
  description: string;
  tags: string[];
  extraTags: number;
  trl: number;
  maturityCaption: string;
  potential: Potential;
  rating: number;
  ratingsCount: number;
  sources: number;
  updatedAt: string;
  status: string;
  /** Развёрнутый набор тегов в карточке справа (если отличается от списочного). */
  drawerTags?: string[];
  drawerExtraTags?: number;
  /** Тексты заданы только для первой карточки; остальные используют общий шаблон. */
  summary?: string;
  problem?: string;
  applicability?: string;
  scenarios?: string[];
};

const TECHNOLOGIES: Technology[] = [
  {
    id: "mms",
    title: "Мобильные модульные подстанции 35–110 кВ",
    description:
      "Модульные подстанции заводской готовности для быстрого развертывания и временного/постоянного электроснабжения.",
    tags: ["Энергетика", "Электроснабжение", "Модульные решения"],
    extraTags: 2,
    trl: 8,
    maturityCaption: "Промышленное применение",
    potential: "Высокий",
    rating: 4.6,
    ratingsCount: 12,
    sources: 17,
    updatedAt: "15.07.2025",
    status: "На рассмотрении",
    drawerTags: ["Энергетика", "Электроснабжение", "Модульные решения", "Временное решение"],
    drawerExtraTags: 1,
    summary:
      "Мобильные модульные подстанции заводской готовности, предназначенные для быстрого развертывания и обеспечения временного или постоянного электроснабжения объектов. Позволяет сократить сроки ввода в эксплуатацию и капитальные затраты.",
    problem:
      "Длительные сроки строительства и высокая стоимость электроснабжения временных объектов, удаленных площадок и объектов в стадии строительства.",
    applicability:
      "Применимо для объектов в стадии строительства, удаленных месторождений, резервного электроснабжения и аварийного восстановления.",
    scenarios: [
      "Резервное электроснабжение объектов добычи",
      "Электроснабжение вахтовых поселков и инфраструктуры",
      "Временное электроснабжение при ремонтах и реконструкции",
      "Быстрое подключение новых объектов",
    ],
  },
  {
    id: "esp",
    title: "Электроцентробежные насосы нового поколения",
    description:
      "Высокоэффективные ЭЦН с постоянными магнитами и интеллектуальным управлением.",
    tags: ["Добыча", "Искусственный подъем", "Энергоэффективность"],
    extraTags: 1,
    trl: 7,
    maturityCaption: "Опытно-промышленная эксплуатация",
    potential: "Средний",
    rating: 4.1,
    ratingsCount: 9,
    sources: 23,
    updatedAt: "12.07.2025",
    status: "На рассмотрении",
  },
  {
    id: "ai-diag",
    title: "AI-платформа предиктивной диагностики",
    description:
      "Система прогнозирования отказов оборудования на основе машинного обучения и IoT данных.",
    tags: ["Цифровизация", "AI/ML", "Предиктивная аналитика"],
    extraTags: 2,
    trl: 6,
    maturityCaption: "Демонстрация в реальных условиях",
    potential: "Высокий",
    rating: 4.8,
    ratingsCount: 15,
    sources: 31,
    updatedAt: "10.07.2025",
    status: "На рассмотрении",
  },
  {
    id: "hybrid",
    title: "Гибридные энергокомплексы с накопителями",
    description:
      "Интеграция ВИЭ с системами накопления энергии для автономного электроснабжения.",
    tags: ["Энергетика", "ВИЭ", "Накопление энергии"],
    extraTags: 1,
    trl: 5,
    maturityCaption: "Испытания в условиях, приближенных к реальным",
    potential: "Средний",
    rating: 3.9,
    ratingsCount: 7,
    sources: 12,
    updatedAt: "08.07.2025",
    status: "На рассмотрении",
  },
  {
    id: "lts",
    title: "Технология низкотемпературной сепарации газа",
    description:
      "Эффективная сепарация газа при низких температурах для арктических условий.",
    tags: ["Добыча", "Подготовка газа", "Арктика"],
    extraTags: 1,
    trl: 7,
    maturityCaption: "Опытно-промышленная эксплуатация",
    potential: "Высокий",
    rating: 4.3,
    ratingsCount: 11,
    sources: 18,
    updatedAt: "05.07.2025",
    status: "На рассмотрении",
  },
  {
    id: "drilling-auto",
    title: "Роботизированные буровые комплексы",
    description:
      "Автоматизация спуско-подъёмных операций и управление режимом бурения без участия человека на буровой площадке.",
    tags: ["Бурение", "Автоматизация", "Промышленная безопасность"],
    extraTags: 2,
    trl: 6,
    maturityCaption: "Опытно-промышленная эксплуатация",
    potential: "Высокий",
    rating: 4.4,
    ratingsCount: 9,
    sources: 21,
    updatedAt: "02.07.2025",
    status: "На рассмотрении",
  },
  {
    id: "ccus",
    title: "Улавливание и захоронение CO₂ (CCUS)",
    description:
      "Комплекс технологий улавливания углекислого газа с промышленных объектов и его закачки в пласт.",
    tags: ["Экология", "Декарбонизация", "Пласт"],
    extraTags: 1,
    trl: 5,
    maturityCaption: "Испытания в условиях, приближенных к реальным",
    potential: "Средний",
    rating: 3.7,
    ratingsCount: 6,
    sources: 27,
    updatedAt: "28.06.2025",
    status: "На рассмотрении",
  },
  {
    id: "digital-twin",
    title: "Цифровой двойник месторождения",
    description:
      "Динамическая модель пласта и наземной инфраструктуры для прогноза добычи и оптимизации режимов.",
    tags: ["Цифровизация", "Моделирование", "Разведка и добыча"],
    extraTags: 2,
    trl: 7,
    maturityCaption: "Опытно-промышленная эксплуатация",
    potential: "Высокий",
    rating: 4.5,
    ratingsCount: 14,
    sources: 33,
    updatedAt: "24.06.2025",
    status: "На рассмотрении",
  },
  {
    id: "smart-well",
    title: "Интеллектуальное заканчивание скважин",
    description:
      "Управляемые клапаны и распределённые датчики для селективного контроля притока по интервалам.",
    tags: ["Добыча", "Заканчивание", "Телеметрия"],
    extraTags: 1,
    trl: 8,
    maturityCaption: "Промышленное применение",
    potential: "Средний",
    rating: 4.0,
    ratingsCount: 10,
    sources: 15,
    updatedAt: "19.06.2025",
    status: "На рассмотрении",
  },
  {
    id: "h2-blend",
    title: "Вдувание водорода в газовые сети",
    description:
      "Смешивание водорода с природным газом для снижения углеродного следа существующей инфраструктуры.",
    tags: ["Энергетика", "Водород", "Декарбонизация"],
    extraTags: 2,
    trl: 4,
    maturityCaption: "Лабораторная проверка",
    potential: "Средний",
    rating: 3.5,
    ratingsCount: 5,
    sources: 19,
    updatedAt: "16.06.2025",
    status: "На рассмотрении",
  },
];

const DRIVERS: Driver[] = [
  { title: "Сокращение сроков ввода в эксплуатацию", value: "на 60-80%" },
  { title: "Снижение капитальных затрат", value: "на 30-40%" },
  { title: "Мобильность и быстрота развертывания", value: "" },
  { title: "Масштабируемость под потребности", value: "" },
];

const DEVELOPERS: Developer[] = [
  { name: "Siemens Energy", initials: "SE", country: "Германия", role: "Разработчик" },
  { name: "Hitachi Energy", initials: "HE", country: "Швейцария", role: "Производитель" },
  { name: "GE Vernova", initials: "GV", country: "США", role: "Производитель" },
];

const KPIS: { value: string; caption: string; delta?: string }[] = [
  { value: "512", caption: "Всего технологий" },
  { value: "128", caption: "На рассмотрении" },
  { value: "84", caption: "С потенциалом" },
  { value: "37", caption: "В работе" },
  { value: "23", caption: "Внедрены" },
  { value: "4.2", caption: "Ср. оценка зрелости", delta: "↑ 0.3" },
];

const FILTERS: { label: string; value: string }[] = [
  { label: "Направление", value: "Все направления" },
  { label: "Зрелость", value: "Все уровни" },
  { label: "Статус", value: "Все статусы" },
  { label: "Потенциал для ГПН", value: "Все уровни" },
];

const CATALOG_TABS = [
  "Все технологии (512)",
  "В работе (37)",
  "С потенциалом (84)",
  "Внедрены (23)",
] as const;

const DRAWER_TABS = ["Обзор", "Описание", "Применение", "Аналитика", "Источники", "Связи"] as const;

type DrawerTab = (typeof DRAWER_TABS)[number];

const GENERIC_SCENARIOS = [
  "Пилотное применение на действующих активах",
  "Тиражирование после подтверждения эффекта",
  "Совместная оценка с профильными подразделениями",
];

const trlPercent = (trl: number) => Math.round((trl / 9) * 100);

const summaryOf = (t: Technology) =>
  t.summary ??
  `${t.description} Карточка технологии заполняется по мере накопления подтверждённых материалов из отраслевых источников.`;

const problemOf = (t: Technology) =>
  t.problem ??
  "Формулировка проблемы уточняется: описание готовится на основе отраслевых источников и экспертных оценок.";

const applicabilityOf = (t: Technology) =>
  t.applicability ??
  "Область применения уточняется: требуется подтверждение эффекта на пилотных объектах.";

/* ------------------------------ parts ------------------------------ */

function PotentialValue({ potential }: { potential: Potential }) {
  const ok = potential === "Высокий";
  return (
    <span className={styles.metricValue}>
      <span className={`${styles.dot} ${ok ? styles.dotOk : styles.dotWarn}`} />
      <span className={ok ? styles.textOk : styles.textWarn}>{potential}</span>
    </span>
  );
}

/** span-based: карточка технологии — <button>, внутрь допустим только phrasing content. */
function Chips({ tags, extra }: { tags: string[]; extra: number }) {
  return (
    <span className={styles.chips}>
      {tags.map((tag) => (
        <span key={tag} className={styles.chip}>
          {tag}
        </span>
      ))}
      {extra > 0 ? <span className={`${styles.chip} ${styles.chipMore}`}>+{extra}</span> : null}
    </span>
  );
}

function TechCard({
  tech,
  selected,
  onSelect,
}: {
  tech: Technology;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={`${styles.techCard} ${selected ? styles.techCardActive : ""}`}
    >
      <span className={styles.thumb}>
        <BoltIcon />
      </span>

      <span className={styles.techMain}>
        <span className={styles.techTitle}>{tech.title}</span>
        <span className={styles.techDesc}>{tech.description}</span>
        <Chips tags={tech.tags} extra={tech.extraTags} />
      </span>

      <span className={styles.metrics}>
        <span className={styles.metric}>
          <span className={styles.metricLabel}>Зрелость</span>
          <span className={styles.metricValue}>TRL {tech.trl}</span>
          <span className={styles.bar}>
            <span className={styles.barFill} style={{ width: `${trlPercent(tech.trl)}%` }} />
          </span>
        </span>

        <span className={styles.metric}>
          <span className={styles.metricLabel}>Потенциал для ГПН</span>
          <PotentialValue potential={tech.potential} />
        </span>

        <span className={styles.metric}>
          <span className={styles.metricLabel}>Оценка</span>
          <span className={styles.metricValue}>
            <StarIcon className={styles.star} />
            {tech.rating.toFixed(1)}
          </span>
          <span className={styles.metricSub}>Источники {tech.sources}</span>
        </span>
      </span>
    </button>
  );
}

function OverviewTab({ tech }: { tech: Technology }) {
  const ok = tech.potential === "Высокий";
  return (
    <div className={styles.overview}>
      <div className={styles.autoCols}>
        <section className={styles.subCard}>
          <h3 className={styles.cardTitle}>Краткое описание</h3>
          <p className={styles.prose}>{summaryOf(tech)}</p>
        </section>

        <section className={styles.subCard}>
          <h3 className={styles.cardTitle}>Проблема, которую решают</h3>
          <p className={styles.prose}>{problemOf(tech)}</p>
          <button type="button" className={styles.linkBtn}>
            Показать подробнее
          </button>
        </section>
      </div>

      <section className={styles.subCard}>
        <h3 className={styles.cardTitle}>Ключевые показатели</h3>
        <div className={styles.kv}>
          <div className={styles.kvRow}>
            <span className={styles.kvLabel}>Зрелость технологии</span>
            <span className={styles.maturityCell}>
              <span className={styles.kvValue}>TRL {tech.trl}</span>
              <span className={styles.kvCaption}>{tech.maturityCaption}</span>
              <span className={`${styles.bar} ${styles.maturityBar}`}>
                <span
                  className={`${styles.barFill} ${styles.barFillOk}`}
                  style={{ width: `${trlPercent(tech.trl)}%` }}
                />
              </span>
            </span>
          </div>

          <div className={styles.kvRow}>
            <span className={styles.kvLabel}>Потенциал для ГПН</span>
            <span className={styles.kvValue}>
              <span className={`${styles.dot} ${ok ? styles.dotOk : styles.dotWarn}`} />
              <span className={ok ? styles.textOk : styles.textWarn}>{tech.potential}</span>
            </span>
          </div>

          <div className={styles.kvRow}>
            <span className={styles.kvLabel}>Оценка экспертов</span>
            <span className={styles.kvValue}>
              <StarIcon className={styles.star} />
              {tech.rating.toFixed(1)}
              <span className={styles.kvMuted}>({tech.ratingsCount} оценок)</span>
            </span>
          </div>

          <div className={styles.kvRow}>
            <span className={styles.kvLabel}>Последнее обновление</span>
            <span className={styles.kvValue}>{tech.updatedAt}</span>
          </div>

          <div className={styles.kvRow}>
            <span className={styles.kvLabel}>Статус в компании</span>
            <span className={styles.kvValue}>
              <span className={`${styles.pill} ${styles.pillWarn}`}>{tech.status}</span>
            </span>
          </div>
        </div>
      </section>

      <div className={styles.counters}>
        {[
          { label: "Примеры применения", value: 12, unit: "реализованных кейсов" },
          { label: "Разработчики и партнёры", value: 8, unit: "компаний" },
          { label: "Драйверы эффективности", value: 5, unit: "ключевых факторов" },
          { label: "Источники", value: tech.sources, unit: "подтвержденных материалов" },
        ].map((c) => (
          <div key={c.label} className={styles.counter}>
            <span className={styles.counterLabel}>{c.label}</span>
            <span className={styles.counterValue}>{c.value}</span>
            <span className={styles.counterUnit}>{c.unit}</span>
          </div>
        ))}
      </div>

      <section className={styles.subCard}>
        <h3 className={styles.cardTitle}>Потенциал применения в ГПН</h3>
        <div className={styles.potentialHead}>
          <span className={`${styles.dot} ${ok ? styles.dotOk : styles.dotWarn}`} />
          <span className={ok ? styles.textOk : styles.textWarn}>{tech.potential} потенциал</span>
        </div>
        <p className={styles.prose}>{applicabilityOf(tech)}</p>
        <p className={styles.subHeading}>Возможные сценарии</p>
        <ul className={styles.checkList}>
          {(tech.scenarios ?? GENERIC_SCENARIOS).map((s) => (
            <li key={s} className={styles.checkItem}>
              <CheckIcon className={styles.checkIcon} />
              <span>{s}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className={styles.subCard}>
        <h3 className={styles.cardTitle}>Драйверы эффективности</h3>
        <div className={styles.driverList}>
          {DRIVERS.map((d) => (
            <div key={d.title} className={styles.driverRow}>
              <span className={styles.driverIcon}>
                <BoltIcon size={14} />
              </span>
              <span className={styles.driverText}>
                <span className={styles.driverTitle}>{d.title}</span>
                {d.value ? <span className={styles.driverValue}>{d.value}</span> : null}
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className={styles.subCard}>
        <h3 className={styles.cardTitle}>Ключевые разработчики</h3>
        <div className={styles.devGrid}>
          {DEVELOPERS.map((d) => (
            <div key={d.name} className={styles.devTile}>
              <span className={styles.avatar}>{d.initials}</span>
              <span className={styles.devText}>
                <span className={styles.devName}>{d.name}</span>
                <span className={styles.devMeta}>
                  {d.country} · {d.role}
                </span>
              </span>
            </div>
          ))}
          <div className={styles.devTile}>
            <span className={styles.avatar}>+5</span>
            <span className={styles.devMeta}>еще компаний</span>
          </div>
        </div>
      </section>
    </div>
  );
}

/* ------------------------------ screen ------------------------------ */

export function TechnologiesPreview() {
  const [selectedId, setSelectedId] = useState<string>(TECHNOLOGIES[0].id);
  const [catalogTab, setCatalogTab] = useState<string>(CATALOG_TABS[0]);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>("Обзор");
  const [gridView, setGridView] = useState(false);

  const selected = TECHNOLOGIES.find((t) => t.id === selectedId) ?? TECHNOLOGIES[0];

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <span className={styles.titleIcon}>
            <BookmarkIcon size={18} />
          </span>
          <h1 className={styles.title}>Технологии</h1>
        </div>
        <span className={styles.badge}>
          <span className={styles.badgeDot} />
          Прототип · демонстрационные данные
        </span>
        <p className={styles.subtitle}>Каталог технологических решений и инноваций</p>
      </header>

      <div className={styles.kpiRow}>
        {KPIS.map((kpi) => (
          <div key={kpi.caption} className={styles.kpiCard}>
            <div className={styles.kpiValueRow}>
              <span className={styles.kpiValue}>{kpi.value}</span>
              {kpi.delta ? <span className={styles.deltaChip}>{kpi.delta}</span> : null}
            </div>
            <span className={styles.kpiCaption}>{kpi.caption}</span>
          </div>
        ))}
      </div>

      <section className={`${styles.card} ${styles.toolbar}`}>
        <div className={styles.searchRow}>
          <div className={styles.searchWrap}>
            <span className={styles.searchIcon}>
              <SearchIcon size={16} />
            </span>
            <input
              type="search"
              className={styles.searchInput}
              placeholder="Поиск по технологиям, проблемам, компаниям..."
              aria-label="Поиск по технологиям"
            />
          </div>
          <button type="button" className={styles.btn}>
            <FilterIcon size={14} />
            Фильтры
          </button>
        </div>

        <div className={styles.filterRow}>
          {FILTERS.map((f) => (
            <label key={f.label} className={styles.field}>
              <span className={styles.fieldLabel}>{f.label}</span>
              <select className={styles.select} defaultValue={f.value}>
                <option>{f.value}</option>
              </select>
            </label>
          ))}
          <button type="button" className={styles.resetLink}>
            Сбросить все
          </button>
        </div>
      </section>

      <div className={styles.tabsRow}>
        <div className={styles.tabs} role="tablist" aria-label="Фильтр по статусу технологий">
          {CATALOG_TABS.map((tab) => (
            <button
              key={tab}
              type="button"
              role="tab"
              aria-selected={catalogTab === tab}
              onClick={() => setCatalogTab(tab)}
              className={`${styles.tab} ${catalogTab === tab ? styles.tabActive : ""}`}
            >
              {tab}
            </button>
          ))}
        </div>

        <div className={styles.tabsAside}>
          <button type="button" className={`${styles.btn} ${styles.sortBtn}`}>
            Сортировка: <span className={styles.sortValue}>Новые сначала</span>
            <ChevronDownIcon />
          </button>
          <div className={styles.viewToggle}>
            <button
              type="button"
              aria-label="Списком"
              aria-pressed={!gridView}
              onClick={() => setGridView(false)}
              className={`${styles.viewBtn} ${!gridView ? styles.viewBtnActive : ""}`}
            >
              <ListViewIcon />
            </button>
            <button
              type="button"
              aria-label="Плиткой"
              aria-pressed={gridView}
              onClick={() => setGridView(true)}
              className={`${styles.viewBtn} ${gridView ? styles.viewBtnActive : ""}`}
            >
              <GridViewIcon />
            </button>
          </div>
        </div>
      </div>

      <div className={styles.layout}>
        <div className={styles.listPane}>
          <div className={styles.list}>
            {TECHNOLOGIES.map((tech) => (
              <TechCard
                key={tech.id}
                tech={tech}
                selected={tech.id === selectedId}
                onSelect={() => setSelectedId(tech.id)}
              />
            ))}
          </div>

          <div className={`${styles.card} ${styles.pagination}`}>
            <span className={styles.pageInfo}>Показано 1-10 из 512</span>
            <div className={styles.pages}>
              {["1", "2", "3"].map((p) => (
                <button
                  key={p}
                  type="button"
                  className={`${styles.pageBtn} ${p === "1" ? styles.pageBtnActive : ""}`}
                >
                  {p}
                </button>
              ))}
              <span className={styles.ellipsis}>…</span>
              <button type="button" className={styles.pageBtn}>
                52
              </button>
              <button type="button" className={styles.pageBtn} aria-label="Следующая страница">
                ›
              </button>
            </div>
          </div>
        </div>

        <aside className={`${styles.card} ${styles.drawer}`}>
          <div className={styles.drawerHead}>
            <button type="button" className={styles.backBtn}>
              <ArrowLeftIcon />
              Назад к списку
            </button>
            <div className={styles.iconBtns}>
              <button type="button" className={styles.iconBtn} aria-label="В закладки">
                <BookmarkIcon size={15} />
              </button>
              <button type="button" className={styles.iconBtn} aria-label="Ещё">
                <DotsIcon size={15} />
              </button>
              <button type="button" className={styles.iconBtn} aria-label="Закрыть">
                <CloseIcon size={15} />
              </button>
            </div>
          </div>

          <h2 className={styles.drawerTitle}>{selected.title}</h2>
          <Chips
            tags={selected.drawerTags ?? selected.tags}
            extra={selected.drawerExtraTags ?? selected.extraTags}
          />

          <div className={styles.drawerTabs} role="tablist" aria-label="Разделы технологии">
            {DRAWER_TABS.map((tab) => (
              <button
                key={tab}
                type="button"
                role="tab"
                aria-selected={drawerTab === tab}
                onClick={() => setDrawerTab(tab)}
                className={`${styles.drawerTab} ${drawerTab === tab ? styles.drawerTabActive : ""}`}
              >
                {tab}
              </button>
            ))}
          </div>

          {drawerTab === "Обзор" ? (
            <OverviewTab tech={selected} />
          ) : (
            <p className={styles.placeholder}>
              Раздел «{drawerTab}» в прототипе не заполнен — содержимое появится в рабочей версии.
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}
