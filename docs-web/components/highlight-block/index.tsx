import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

type SemanticColor = "info" | "warning" | "error" | "default";

interface HighlightProps {
  icon?: string | ReactNode;
  title: string;
  description?: ReactNode;
  color?: SemanticColor | string;
  className?: string;
}

const semanticColors: Record<SemanticColor, { bg: string; border: string; iconBg: string }> = {
  info: {
    bg: "bg-blue-50 dark:bg-blue-950",
    border: "border-blue-200 dark:border-blue-800",
    iconBg: "bg-blue-100 dark:bg-blue-900",
  },
  warning: {
    bg: "bg-amber-50 dark:bg-amber-950",
    border: "border-amber-200 dark:border-amber-800",
    iconBg: "bg-amber-100 dark:bg-amber-900",
  },
  error: {
    bg: "bg-red-50 dark:bg-red-950",
    border: "border-red-200 dark:border-red-800",
    iconBg: "bg-red-100 dark:bg-red-900",
  },
  default: {
    bg: "bg-zinc-50 dark:bg-zinc-900",
    border: "border-zinc-200 dark:border-zinc-800",
    iconBg: "bg-zinc-100 dark:bg-zinc-800",
  },
};

function isHexColor(color: string): boolean {
  return /^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$/.test(color);
}

function hexToRgba(hex: string, alpha: number): string {
  const raw = hex.replace("#", "");
  const expanded =
    raw.length === 3
      ? raw
          .split("")
          .map((c) => c + c)
          .join("")
      : raw;

  const r = parseInt(expanded.slice(0, 2), 16);
  const g = parseInt(expanded.slice(2, 4), 16);
  const b = parseInt(expanded.slice(4, 6), 16);

  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function IconRenderer({ icon }: { icon: string | ReactNode }) {
  if (typeof icon === "string") {
    return <span className="text-2xl leading-none">{icon}</span>;
  }

  return <span className="text-foreground [&>svg]:size-5">{icon}</span>;
}

export function HighlightBlock({ icon, title, description, color = "default", className }: HighlightProps) {
  const isCustom = isHexColor(color);

  const semantic = !isCustom ? semanticColors[(color as SemanticColor) || "default"] : semanticColors.default;

  const customStyles = isCustom
    ? {
        backgroundColor: hexToRgba(color, 0.08),
        borderColor: hexToRgba(color, 0.25),
      }
    : undefined;

  const customIconStyles = isCustom
    ? {
        backgroundColor: hexToRgba(color, 0.15),
      }
    : undefined;

  return (
    <div
      className={cn(
        "flex items-start gap-4 rounded-lg border p-4",
        !isCustom && semantic.bg,
        !isCustom && semantic.border,
        className,
      )}
      style={customStyles}
    >
      {icon && (
        <div
          className={cn(
            "flex size-10 shrink-0 items-center justify-center rounded-md",
            !isCustom && semantic.iconBg,
          )}
          style={customIconStyles}
        >
          <IconRenderer icon={icon} />
        </div>
      )}

      <div className="min-w-0 flex-1">
        <p className="font-semibold text-foreground">{title}</p>
        {description && <div className="mt-1 text-sm text-muted-foreground">{description}</div>}
      </div>
    </div>
  );
}
