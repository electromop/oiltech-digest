import { useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import styles from "./AnalyticsPreview.module.css";

/**
 * Превью-экран «Аналитика для БРБ».
 *
 * ВАЖНО: это статический прототип. Все цифры ниже — вымышленные литералы,
 * никаких обращений к API здесь нет и быть не должно. Экран обязан рендериться
 * одинаково в офлайне. Плашка «Прототип · демонстрационные данные» под
 * заголовком — обязательна, чтобы никто не принял эти числа за живую аналитику.
 */

/* ------------------------------------------------------------------ */
/* Палитра сущностей: цвет закреплён за сущностью, а не за местом на экране */
/* ------------------------------------------------------------------ */

const ENTITY = {
  all: "#2563EB", // Все сигналы / Новые технологии
  potential: "#0D9488", // Сигналы с потенциалом
  business: "#7C3AED", // Бизнес-сигналы
  companies: "#0891B2", // Компании-новички
  deals: "#DB2777", // Инвестиции и сделки
  digest: "#4F46E5", // Дайджесты
} as const;

const TINT = {
  all: "rgba(37, 99, 235, 0.12)",
  potential: "rgba(13, 148, 136, 0.12)",
  business: "rgba(124, 58, 237, 0.12)",
  companies: "rgba(8, 145, 178, 0.12)",
  deals: "rgba(219, 39, 119, 0.12)",
  digest: "rgba(79, 70, 229, 0.12)",
} as const;

type EntityKey = keyof typeof ENTITY;

/* ------------------------------------------------------------------ */
/* Иконки                                                              */
/* ------------------------------------------------------------------ */

const ICONS = {
  chip: [
    "M7 7h10v10H7z",
    "M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4",
  ],
  trend: ["M3 17l6-6 4 4 8-8", "M15 7h6v6"],
  briefcase: ["M3 7h18v13H3z", "M8 7V5a2 2 0 012-2h4a2 2 0 012 2v2", "M3 12h18"],
  building: [
    "M4 21V6a1 1 0 011-1h8a1 1 0 011 1v15",
    "M14 10h5a1 1 0 011 1v10",
    "M2 21h20",
    "M7 9h3M7 13h3M7 17h3",
  ],
  coins: [
    "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z",
    "M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6",
    "M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6",
  ],
  doc: ["M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z", "M14 3v5h5", "M9 13h6M9 17h4"],
  calendar: ["M3 5h18v16H3z", "M8 3v4M16 3v4M3 10h18"],
  caret: ["M6 9l6 6 6-6"],
  handshake: ["M8 12l3 3 2-2 3 3", "M2 9l4-3 6 2 6-2 4 3", "M2 9v6l4 4M22 9v6l-4 4"],
  cube: ["M12 2l9 5v10l-9 5-9-5V7z", "M3 7l9 5 9-5", "M12 12v10"],
  clipboard: [
    "M9 3h6v3H9z",
    "M9 4.5H6a1 1 0 00-1 1V20a1 1 0 001 1h12a1 1 0 001-1V5.5a1 1 0 00-1-1h-3",
    "M9 12h6M9 16h4",
  ],
  globe: ["M12 3a9 9 0 100 18 9 9 0 000-18z", "M3 12h18", "M12 3c2.5 2.4 3.8 5.4 3.8 9S14.5 18.6 12 21c-2.5-2.4-3.8-5.4-3.8-9S9.5 5.4 12 3z"],
  arrowDown: ["M12 4v14", "M6 12l6 6 6-6"],
  bolt: ["M13 2L4 14h7l-1 8 9-12h-7z"],
  clock: ["M12 3a9 9 0 100 18 9 9 0 000-18z", "M12 7v5l3 2"],
  factory: ["M3 21V10l6 4V10l6 4V7l6 4v10z", "M3 21h18", "M7 17h2M13 17h2"],
  leaf: ["M4 20C4 10 11 4 20 4c0 9-6 16-16 16z", "M4 20c4-4 7-7 12-9"],
  battery: ["M2 8h16v8H2z", "M18 11h3v2h-3", "M5 11h6"],
  cpu: ["M8 8h8v8H8z", "M4 4h16v16H4z", "M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"],
  bookmark: ["M6 4a2 2 0 012-2h8a2 2 0 012 2v17l-6-4-6 4z"],
} as const;

type IconName = keyof typeof ICONS;

function Glyph({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {ICONS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

function CardHead({ entity, title, sub }: { entity?: EntityKey; title: string; sub?: string }) {
  return (
    <div className={styles.cardHead}>
      <span className={styles.cardHeadMain}>
        {entity ? <span className={styles.entityDot} style={{ background: ENTITY[entity] }} /> : null}
        <h2 className={styles.cardTitle}>{title}</h2>
      </span>
      {sub ? <span className={styles.cardSub}>{sub}</span> : null}
    </div>
  );
}

function Star({ size = 15 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M12 2.6l2.9 5.9 6.5.9-4.7 4.6 1.1 6.5-5.8-3.1-5.8 3.1 1.1-6.5-4.7-4.6 6.5-.9z" />
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/* Форматирование                                                      */
/* ------------------------------------------------------------------ */

/** Неразрывный пробел как разделитель разрядов — чтобы число не рвалось по строкам. */
function fmt(value: number): string {
  return value.toLocaleString("ru-RU").replace(/\s/g, " ");
}

/* ------------------------------------------------------------------ */
/* Спарклайны                                                          */
/* ------------------------------------------------------------------ */

const SPARK_W = 120;
const SPARK_H = 34;

function sparkGeometry(values: number[]): { line: string; area: string } {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 3;
  const usable = SPARK_H - pad * 2;
  const step = values.length > 1 ? SPARK_W / (values.length - 1) : SPARK_W;
  const points = values.map((value, index) => {
    const x = index * step;
    const y = pad + (1 - (value - min) / span) * usable;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
  const line = `M${points.join("L")}`;
  return { line, area: `${line}L${SPARK_W},${SPARK_H}L0,${SPARK_H}Z` };
}

function Sparkline({ values, color }: { values: number[]; color: string }) {
  const { line, area } = sparkGeometry(values);
  return (
    <svg
      className={styles.spark}
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
    >
      <path d={area} fill={color} fillOpacity={0.1} />
      <path
        d={line}
        fill="none"
        stroke={color}
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/* Данные (вымышленные)                                                */
/* ------------------------------------------------------------------ */

type Kpi = {
  id: string;
  label: string;
  value: string;
  delta: string | null;
  entity: EntityKey;
  icon: IconName;
  series: number[];
};

const KPIS: Kpi[] = [
  {
    id: "tech",
    label: "Новые технологии",
    value: "680",
    delta: "+12%",
    entity: "all",
    icon: "chip",
    series: [498, 512, 505, 534, 551, 545, 572, 590, 583, 612, 634, 651, 664, 680],
  },
  {
    id: "potential",
    label: "Сигналы с потенциалом",
    value: fmt(1232),
    delta: "+8%",
    entity: "potential",
    icon: "trend",
    series: [1010, 1035, 1022, 1064, 1088, 1075, 1110, 1132, 1120, 1158, 1180, 1201, 1218, 1232],
  },
  {
    id: "business",
    label: "Бизнес-сигналы",
    value: "156",
    delta: "+24%",
    entity: "business",
    icon: "briefcase",
    series: [96, 102, 99, 110, 118, 114, 124, 131, 127, 138, 144, 149, 152, 156],
  },
  {
    id: "companies",
    label: "Компании-новички",
    value: "47",
    delta: "+15%",
    entity: "companies",
    icon: "building",
    series: [28, 31, 30, 33, 35, 34, 37, 39, 38, 41, 43, 45, 46, 47],
  },
  {
    id: "deals",
    label: "Инвестиции и сделки",
    value: "32",
    delta: "+18%",
    entity: "deals",
    icon: "coins",
    series: [18, 20, 19, 22, 23, 22, 25, 26, 25, 28, 29, 30, 31, 32],
  },
  {
    id: "digest",
    label: "Дайджесты сформировано",
    value: "6",
    delta: null,
    entity: "digest",
    icon: "doc",
    series: [2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 5, 6, 6, 6],
  },
];

const DIRECTIONS: { name: string; value: number }[] = [
  { name: "Энергетика и энергосистемы", value: 1432 },
  { name: "Цифровизация и ИИ", value: 1128 },
  { name: "Промышленная безопасность", value: 986 },
  { name: "Разведка и добыча", value: 872 },
  { name: "Оборудование и материалы", value: 765 },
  { name: "Инжиниринг и строительство", value: 612 },
  { name: "Экология и углеродный менеджмент", value: 548 },
  { name: "Транспорт и логистика", value: 431 },
];

const TIMELINE_DATES = [
  "1 мая",
  "3 мая",
  "5 мая",
  "8 мая",
  "10 мая",
  "12 мая",
  "15 мая",
  "17 мая",
  "19 мая",
  "22 мая",
  "24 мая",
  "26 мая",
  "28 мая",
  "29 мая",
  "30 мая",
  "31 мая",
];

/** Подписи оси X — только опорные даты, а не каждая точка. */
const AXIS_TICKS = [0, 3, 6, 9, 15];

type Series = { key: EntityKey; label: string; values: number[] };

const SERIES: Series[] = [
  {
    key: "all",
    label: "Все сигналы",
    values: [900, 940, 915, 980, 1010, 990, 1060, 1105, 1080, 1160, 1215, 1190, 1270, 1320, 1355, 1400],
  },
  {
    key: "potential",
    label: "Сигналы с потенциалом",
    values: [600, 625, 640, 660, 655, 690, 705, 640, 585, 610, 665, 700, 725, 745, 760, 780],
  },
  {
    key: "business",
    label: "Бизнес-сигналы",
    values: [300, 312, 305, 328, 340, 335, 352, 366, 358, 375, 388, 380, 396, 405, 412, 420],
  },
];

/** Последовательная одноцветная шкала (светлее → темнее = слабее → активнее). */
const RAMP = ["#DBEAFE", "#BFDBFE", "#93C5FD", "#60A5FA", "#3B82F6", "#2563EB", "#1D4ED8"];

type RegionId = "na" | "la" | "eu" | "af" | "me" | "ru" | "as";

const REGIONS: { id: RegionId; name: string; value: number; shade: string }[] = [
  { id: "na", name: "Северная Америка", value: 2341, shade: "#2563EB" },
  { id: "eu", name: "Европа", value: 2876, shade: "#1D4ED8" },
  { id: "as", name: "Азия", value: 1654, shade: "#60A5FA" },
  { id: "me", name: "Ближний Восток", value: 1203, shade: "#BFDBFE" },
  { id: "ru", name: "Россия и СНГ", value: 1987, shade: "#3B82F6" },
  { id: "af", name: "Африка", value: 412, shade: "#DBEAFE" },
  { id: "la", name: "Латинская Америка", value: 1356, shade: "#93C5FD" },
];

/** Схематичные «кляксы» материков — это не картография, а условная подложка:
 *  важно только взаимное расположение регионов, а не их реальные очертания. */
const MAP_PATHS: Record<RegionId, string> = {
  na: "M26 36C32 22 50 15 72 17C90 19 105 25 106 36C107 46 97 53 88 59C80 65 78 76 69 82C60 88 50 82 45 71C39 59 20 48 26 36Z",
  la: "M74 92C86 85 100 91 99 103C98 114 91 121 89 131C87 142 81 153 75 149C69 145 71 131 71 120C71 109 64 98 74 92Z",
  eu: "M130 28C141 19 159 21 170 25C181 29 184 40 177 47C170 54 156 56 145 53C133 50 121 37 130 28Z",
  af: "M137 63C152 56 172 58 179 69C186 80 177 91 173 101C168 112 162 125 152 131C141 137 135 123 135 110C135 97 126 71 137 63Z",
  me: "M187 60C195 54 205 58 207 66C209 75 200 83 192 81C183 78 178 65 187 60Z",
  ru: "M180 16C201 8 241 10 263 17C281 22 289 31 280 39C271 47 246 49 224 47C202 45 184 39 178 31C172 23 174 19 180 16Z",
  as: "M211 54C228 47 255 52 267 62C277 71 272 83 260 89C247 95 235 97 227 92C219 87 209 76 207 66C206 58 207 55 211 54Z",
};

const OPPORTUNITIES: { title: string; count: number; icon: IconName }[] = [
  { title: "Снижение CAPEX при энергетическом строительстве", count: 25, icon: "arrowDown" },
  { title: "Повышение надёжности энергоснабжения", count: 18, icon: "bolt" },
  { title: "Сокращение сроков ввода объектов", count: 17, icon: "clock" },
  { title: "Импортозамещение и локализация", count: 14, icon: "factory" },
  { title: "Декарбонизация и устойчивое развитие", count: 12, icon: "leaf" },
];

const WATCHLIST: { title: string; caption: string; icon: IconName }[] = [
  { title: "Мобильные модульные подстанции", caption: "Высокий потенциал", icon: "cube" },
  { title: "Накопители энергии LFP", caption: "Средний потенциал", icon: "battery" },
  { title: "AI-платформы предиктивной аналитики", caption: "Высокий потенциал", icon: "cpu" },
];

const TRENDS: { title: string; growth: string }[] = [
  { title: "Мобильные и модульные энергосистемы", growth: "86%" },
  { title: "Гибридные энергетические комплексы", growth: "72%" },
  { title: "Цифровые двойники и моделирование", growth: "68%" },
  { title: "Автономные системы управления", growth: "61%" },
  { title: "Накопители энергии и системы хранения", growth: "59%" },
];

const BUSINESS_SIGNALS: { label: string; count: number; growth: string; icon: IconName }[] = [
  { label: "Сделки и партнёрства", count: 18, growth: "29%", icon: "handshake" },
  { label: "Инвестиции и финансирование", count: 9, growth: "13%", icon: "coins" },
  { label: "Новые продукты и решения", count: 41, growth: "24%", icon: "cube" },
  { label: "Тендеры и контракты", count: 36, growth: "33%", icon: "clipboard" },
  { label: "Выход на новые рынки", count: 52, growth: "22%", icon: "globe" },
];

const DEALS: {
  initials: string;
  title: string;
  description: string;
  region: string;
  amount: string;
  type: string;
  date: string;
}[] = [
  {
    initials: "AS",
    title: "ACWA Power & Siemens Energy",
    description: "Строительство гибридной электростанции 2 ГВт",
    region: "Саудовская Аравия",
    amount: "2,1 млрд $",
    type: "Инвестиция",
    date: "23.05.2025",
  },
  {
    initials: "EL",
    title: "ExxonMobil & Lithium Americas",
    description: "Партнерство по проекту Thacker Pass Lithium",
    region: "США",
    amount: "1,3 млрд $",
    type: "Инвестиция",
    date: "15.05.2025",
  },
  {
    initials: "TS",
    title: "TotalEnergies & SunPower",
    description: "Приобретение портфеля солнечных проектов 1,2 ГВт",
    region: "Европа",
    amount: "0,9 млрд $",
    type: "Сделка",
    date: "08.05.2025",
  },
];

const REPORTS: { title: string; date: string }[] = [
  { title: "Тренды в энергетике 2025–2030", date: "30.05.2025" },
  { title: "Рынок систем хранения энергии", date: "27.05.2025" },
  { title: "Мировой рынок модульных решений", date: "24.05.2025" },
];

const TABS = [
  "Обзор",
  "Технологические тренды",
  "Бизнес-сигналы",
  "Компании и рынки",
  "Возможности для ГПН",
  "Benchmark",
];

const WATCH_TABS = ["Технологии", "Компании", "Рынки"];

/* ------------------------------------------------------------------ */
/* Линейный график                                                     */
/* ------------------------------------------------------------------ */

const CHART_W = 560;
const CHART_H = 230;
const PAD_L = 38;
const PAD_R = 10;
const PAD_T = 10;
const PAD_B = 26;
const PLOT_W = CHART_W - PAD_L - PAD_R;
const PLOT_H = CHART_H - PAD_T - PAD_B;
const Y_MAX = 1500;
const Y_TICKS = [0, 300, 600, 900, 1200, 1500];
const POINTS = TIMELINE_DATES.length;

function xAt(index: number): number {
  return PAD_L + (index / (POINTS - 1)) * PLOT_W;
}

function yAt(value: number): number {
  return PAD_T + (1 - value / Y_MAX) * PLOT_H;
}

function linePath(values: number[]): string {
  return `M${values.map((value, index) => `${xAt(index).toFixed(2)},${yAt(value).toFixed(2)}`).join("L")}`;
}

function SignalsChart() {
  const [hover, setHover] = useState<number | null>(null);

  function handleMove(event: ReactMouseEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width === 0) return;
    const x = ((event.clientX - rect.left) / rect.width) * CHART_W;
    const ratio = (x - PAD_L) / PLOT_W;
    const index = Math.round(ratio * (POINTS - 1));
    setHover(Math.min(POINTS - 1, Math.max(0, index)));
  }

  const flip = hover !== null && hover > POINTS - 5;

  return (
    <div className={styles.chartWrap}>
      <svg
        className={styles.chart}
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        onMouseMove={handleMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label="Динамика сигналов за май 2025 года: все сигналы, сигналы с потенциалом, бизнес-сигналы"
      >
        {/* Сетка — только горизонтальная и подчёркнуто ненавязчивая */}
        {Y_TICKS.map((tick) => (
          <g key={tick}>
            <line className={styles.gridLine} x1={PAD_L} y1={yAt(tick)} x2={CHART_W - PAD_R} y2={yAt(tick)} />
            <text className={styles.axisText} x={PAD_L - 8} y={yAt(tick)} textAnchor="end" dominantBaseline="middle">
              {tick}
            </text>
          </g>
        ))}

        {AXIS_TICKS.map((index) => (
          <text
            key={index}
            className={styles.axisText}
            x={xAt(index)}
            y={CHART_H - 8}
            textAnchor={index === 0 ? "start" : index === POINTS - 1 ? "end" : "middle"}
          >
            {TIMELINE_DATES[index]}
          </text>
        ))}

        {hover !== null && (
          <line className={styles.crosshair} x1={xAt(hover)} y1={PAD_T} x2={xAt(hover)} y2={PAD_T + PLOT_H} />
        )}

        {SERIES.map((series) => (
          <path
            key={series.key}
            d={linePath(series.values)}
            fill="none"
            stroke={ENTITY[series.key]}
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        ))}

        {hover !== null &&
          SERIES.map((series) => (
            <circle
              key={series.key}
              cx={xAt(hover)}
              cy={yAt(series.values[hover])}
              r={3.5}
              fill="#ffffff"
              stroke={ENTITY[series.key]}
              strokeWidth={2}
              vectorEffect="non-scaling-stroke"
            />
          ))}
      </svg>

      {hover !== null && (
        <div
          className={`${styles.tooltip} ${flip ? styles.tooltipFlip : ""}`}
          style={{ left: `${(xAt(hover) / CHART_W) * 100}%` }}
        >
          <div className={styles.tooltipDate}>{TIMELINE_DATES[hover]}</div>
          {SERIES.map((series) => (
            <div key={series.key} className={styles.tooltipRow}>
              <span className={styles.legendDot} style={{ background: ENTITY[series.key] }} />
              {series.label}
              <span className={styles.tooltipValue}>{fmt(series.values[hover])}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Экран                                                               */
/* ------------------------------------------------------------------ */

export function AnalyticsPreview() {
  const [tab, setTab] = useState(TABS[0]);
  const [watchTab, setWatchTab] = useState(WATCH_TABS[0]);

  const maxDirection = Math.max(...DIRECTIONS.map((item) => item.value));

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleWrap}>
          <div className={styles.titleRow}>
            <span className={styles.titleIcon}>
              <Glyph name="bookmark" size={22} />
            </span>
            <h1 className={styles.h1}>Аналитика для БРБ</h1>
          </div>
          <span className={styles.protoBadge}>
            <span className={styles.protoDot} />
            Прототип · демонстрационные данные
          </span>
          <p className={styles.subtitle}>Технологические тренды, бизнес-сигналы и рыночная аналитика</p>
        </div>

        <div className={styles.headerActions}>
          <span className={styles.datePill}>
            <Glyph name="calendar" size={15} />
            01.05.2025 — 31.05.2025
          </span>
          <button type="button" className={styles.btn}>
            Настроить вид
          </button>
          <button type="button" className={`${styles.btn} ${styles.btnPrimary}`}>
            Экспорт
            <Glyph name="caret" size={13} />
          </button>
        </div>
      </header>

      <nav className={styles.tabs}>
        {TABS.map((item) => (
          <button
            key={item}
            type="button"
            className={`${styles.tab} ${item === tab ? styles.tabActive : ""}`}
            aria-current={item === tab ? "page" : undefined}
            onClick={() => setTab(item)}
          >
            {item}
          </button>
        ))}
      </nav>

      {tab !== TABS[0] ? (
        <section className={styles.card}>
          <p className={styles.placeholder}>Раздел прототипа — данные появятся после согласования состава метрик</p>
        </section>
      ) : (
        <>
          {/* ---------------- KPI ---------------- */}
          <section className={styles.kpiGrid}>
            {KPIS.map((kpi) => (
              <article key={kpi.id} className={`${styles.card} ${styles.kpiCard}`}>
                <div className={styles.kpiTop}>
                  <span
                    className={styles.kpiIcon}
                    style={{ background: TINT[kpi.entity], color: ENTITY[kpi.entity] }}
                  >
                    <Glyph name={kpi.icon} size={17} />
                  </span>
                  <span className={styles.kpiLabel}>{kpi.label}</span>
                </div>
                <div className={styles.kpiValueRow}>
                  <span className={styles.kpiValue}>{kpi.value}</span>
                  {kpi.delta ? (
                    <span className={`${styles.chip} ${styles.chipOk}`}>{kpi.delta}</span>
                  ) : (
                    <span className={styles.deltaNone}>–</span>
                  )}
                </div>
                <span className={styles.kpiCaption}>в сравнении с апрелем</span>
                <Sparkline values={kpi.series} color={ENTITY[kpi.entity]} />
              </article>
            ))}
          </section>

          {/* ---------------- Ряд 2 ---------------- */}
          <section className={styles.row2}>
            {/* (a) Топ направлений */}
            <article className={styles.card}>
              <CardHead entity="all" title="Топ направлений по активности" sub="по количеству сигналов" />
              <div className={styles.barList}>
                {DIRECTIONS.map((item) => (
                  <div key={item.name} className={styles.barRow}>
                    <span className={styles.barLabel} title={item.name}>
                      {item.name}
                    </span>
                    <span className={styles.barTrack}>
                      <span
                        className={styles.barFill}
                        style={{
                          display: "block",
                          width: `${(item.value / maxDirection) * 100}%`,
                          background: ENTITY.all,
                        }}
                      />
                    </span>
                    <span className={styles.barValue}>{fmt(item.value)}</span>
                  </div>
                ))}
              </div>
              <button type="button" className={styles.cardLink}>
                Смотреть все направления
              </button>
            </article>

            {/* (b) Динамика сигналов */}
            <article className={styles.card}>
              <CardHead title="Динамика сигналов" />
              <div className={styles.legend}>
                {SERIES.map((series) => (
                  <span key={series.key} className={styles.legendItem}>
                    <span className={styles.legendDot} style={{ background: ENTITY[series.key] }} />
                    {series.label}
                  </span>
                ))}
              </div>
              <SignalsChart />
              <button type="button" className={styles.cardLink}>
                Перейти к аналитике
              </button>
            </article>

            {/* (c) Карта активности */}
            <article className={`${styles.card} ${styles.mapCard}`}>
              <CardHead title="Карта активности по регионам" />
              <div className={styles.mapBody}>
                <div className={styles.mapFigure}>
                  <svg
                    className={styles.map}
                    viewBox="0 0 300 160"
                    role="img"
                    aria-label="Схематичная карта активности по регионам: чем темнее, тем выше активность"
                  >
                    <rect x="0" y="0" width="300" height="160" rx="10" fill="rgba(148, 163, 184, 0.09)" />
                    {REGIONS.map((region) => (
                      <path key={region.id} className={styles.mapBlob} d={MAP_PATHS[region.id]} fill={region.shade}>
                        <title>{`${region.name}: ${fmt(region.value)}`}</title>
                      </path>
                    ))}
                  </svg>
                  <div className={styles.rampRow}>
                    <span>меньше</span>
                    <span className={styles.rampBar}>
                      {RAMP.map((shade) => (
                        <span key={shade} className={styles.rampCell} style={{ background: shade }} />
                      ))}
                    </span>
                    <span>больше</span>
                  </div>
                </div>

                <div className={styles.regionList}>
                  {REGIONS.map((region) => (
                    <div key={region.id} className={styles.regionRow}>
                      <span className={styles.legendDot} style={{ background: region.shade }} />
                      <span className={styles.regionName} title={region.name}>
                        {region.name}
                      </span>
                      <span className={styles.regionValue}>{fmt(region.value)}</span>
                    </div>
                  ))}
                </div>
              </div>
              <button type="button" className={styles.cardLink}>
                Смотреть детально
              </button>
            </article>

            {/* (d) Правая колонка */}
            <div className={styles.rail}>
              <article className={styles.card}>
                <CardHead title="Возможности для ГПН" sub="топ актуальных направлений" />
                <div className={styles.rowList}>
                  {OPPORTUNITIES.map((item) => (
                    <div key={item.title} className={styles.listRow}>
                      <span className={styles.tile}>
                        <Glyph name={item.icon} size={15} />
                      </span>
                      <span className={styles.rowTitle}>{item.title}</span>
                      <span className={`${styles.chip} ${styles.chipBrand}`}>{item.count}</span>
                    </div>
                  ))}
                </div>
                <button type="button" className={styles.cardLink}>
                  Смотреть все возможности
                </button>
              </article>

              <article className={styles.card}>
                <CardHead title="Watch-list" />
                <div className={styles.seg}>
                  {WATCH_TABS.map((item) => (
                    <button
                      key={item}
                      type="button"
                      className={`${styles.segBtn} ${item === watchTab ? styles.segBtnActive : ""}`}
                      aria-pressed={item === watchTab}
                      onClick={() => setWatchTab(item)}
                    >
                      {item}
                    </button>
                  ))}
                </div>
                {watchTab === WATCH_TABS[0] ? (
                  <div className={styles.rowList}>
                    {WATCHLIST.map((item) => (
                      <div key={item.title} className={styles.listRow}>
                        <span className={styles.tile}>
                          <Glyph name={item.icon} size={15} />
                        </span>
                        <span>
                          <span className={styles.rowTitle}>{item.title}</span>
                          <span className={styles.rowCaption} style={{ display: "block" }}>
                            {item.caption}
                          </span>
                        </span>
                        <span className={styles.star}>
                          <Star />
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className={styles.segPlaceholder}>Раздел прототипа — список пока не заполнен</p>
                )}
                <button type="button" className={styles.cardLink}>
                  Перейти к watch-list
                </button>
              </article>
            </div>
          </section>

          {/* ---------------- Ряд 3 ---------------- */}
          <section className={styles.row3}>
            {/* (a) Тренды */}
            <article className={styles.card}>
              <CardHead entity="all" title="Ключевые технологические тренды" sub="за период" />
              <div className={styles.rowList}>
                {TRENDS.map((item, index) => (
                  <div key={item.title} className={styles.listRow}>
                    <span className={styles.rank}>{index + 1}</span>
                    <span className={styles.rowTitle}>{item.title}</span>
                    <span className={`${styles.chip} ${styles.chipOk}`}>↑{item.growth}</span>
                  </div>
                ))}
              </div>
              <button type="button" className={styles.cardLink}>
                Все тренды и аналитика
              </button>
            </article>

            {/* (b) Бизнес-сигналы */}
            <article className={styles.card}>
              <CardHead entity="business" title="Бизнес-сигналы" sub="за период" />
              <div className={styles.rowList}>
                {BUSINESS_SIGNALS.map((item) => (
                  <div key={item.label} className={styles.listRow}>
                    <span
                      className={styles.tile}
                      style={{ background: TINT.business, color: ENTITY.business }}
                    >
                      <Glyph name={item.icon} size={15} />
                    </span>
                    <span className={styles.rowTitle}>{item.label}</span>
                    <span className={styles.signalRight}>
                      <span className={styles.signalCount}>{item.count}</span>
                      <span className={`${styles.chip} ${styles.chipOk}`}>↑{item.growth}</span>
                    </span>
                  </div>
                ))}
              </div>
              <button type="button" className={styles.cardLink}>
                Все бизнес-сигналы
              </button>
            </article>

            {/* (c) Сделки */}
            <article className={styles.card}>
              <CardHead entity="deals" title="Крупнейшие сделки и инвестиции" sub="за период" />
              <div className={styles.dealList}>
                {DEALS.map((deal) => (
                  <div key={deal.title} className={styles.dealRow}>
                    <span className={styles.logo} aria-hidden="true">
                      {deal.initials}
                    </span>
                    <span className={styles.dealMain}>
                      <span className={styles.dealTitle} style={{ display: "block" }}>
                        {deal.title}
                      </span>
                      <span className={styles.dealDesc} style={{ display: "block" }}>
                        {deal.description}
                      </span>
                      <span className={styles.dealRegion} style={{ display: "block" }}>
                        {deal.region}
                      </span>
                    </span>
                    <span className={styles.dealSide}>
                      <span className={styles.dealAmount} style={{ display: "block" }}>
                        {deal.amount}
                      </span>
                      <span className={styles.dealType} style={{ display: "block" }}>
                        {deal.type}
                      </span>
                      <span className={styles.dealDate} style={{ display: "block" }}>
                        {deal.date}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
              <button type="button" className={styles.cardLink}>
                Все сделки и инвестиции
              </button>
            </article>

            {/* (d) Отчёты */}
            <div className={styles.rail}>
              <article className={styles.card}>
                <CardHead entity="digest" title="Последние аналитические отчёты" />
                <div className={styles.rowList}>
                  {REPORTS.map((report) => (
                    <div key={report.title} className={styles.listRow}>
                      <span className={styles.tile} style={{ background: TINT.digest, color: ENTITY.digest }}>
                        <Glyph name="doc" size={15} />
                      </span>
                      <span className={styles.rowTitle}>{report.title}</span>
                      <span className={styles.rowMeta}>{report.date}</span>
                    </div>
                  ))}
                </div>
                <button type="button" className={styles.cardLink}>
                  Все отчёты
                </button>
              </article>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
