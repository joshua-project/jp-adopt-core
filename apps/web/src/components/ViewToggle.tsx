"use client";

export type ViewMode = "table" | "kanban";

export interface ViewToggleProps {
  value: ViewMode;
  onChange: (next: ViewMode) => void;
}

/**
 * Pair of small icon buttons that flip between table and kanban
 * presentations of the same underlying list.
 *
 * Choice is stored by the parent; this is a pure-presentational
 * control so the URL or localStorage owns persistence.
 */
export function ViewToggle({ value, onChange }: ViewToggleProps) {
  return (
    <div
      className="inline-flex overflow-hidden rounded-md border border-slate-200 bg-white"
      role="group"
      aria-label="View mode"
    >
      <button
        type="button"
        onClick={() => onChange("table")}
        aria-pressed={value === "table"}
        className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition ${
          value === "table"
            ? "bg-slate-900 text-white"
            : "text-slate-600 hover:bg-slate-50"
        }`}
      >
        <TableIcon />
        Table
      </button>
      <button
        type="button"
        onClick={() => onChange("kanban")}
        aria-pressed={value === "kanban"}
        className={`inline-flex items-center gap-1.5 border-l border-slate-200 px-3 py-1.5 text-xs font-medium transition ${
          value === "kanban"
            ? "bg-slate-900 text-white"
            : "text-slate-600 hover:bg-slate-50"
        }`}
      >
        <KanbanIcon />
        Kanban
      </button>
    </div>
  );
}

function TableIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <rect x="2" y="3" width="12" height="10" rx="1" />
      <path d="M2 7h12M2 10h12" />
    </svg>
  );
}

function KanbanIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <rect x="2" y="3" width="3" height="10" rx="0.5" />
      <rect x="6.5" y="3" width="3" height="7" rx="0.5" />
      <rect x="11" y="3" width="3" height="5" rx="0.5" />
    </svg>
  );
}
