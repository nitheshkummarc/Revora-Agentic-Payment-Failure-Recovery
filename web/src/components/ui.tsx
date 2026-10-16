import { ArrowUpRight } from "@phosphor-icons/react";
import type { ReactNode } from "react";
import type { Outcome } from "../types";
import { OUTCOME_LABELS } from "../lib/catalogue";

export const SOURCE_URL = "https://github.com/nitheshkummarc/Revora-Agentic-Payment-Failure-Recovery";

const pill =
  "group inline-flex items-center gap-3 rounded-full py-2 pl-5 pr-2 text-sm font-medium " +
  "transition-[transform,background-color,color] duration-500 ease-spring active:scale-[0.98] " +
  "disabled:cursor-not-allowed disabled:opacity-50";

const variants = {
  primary: "bg-ink text-bg hover:bg-accent hover:text-accent-ink",
  quiet: "bg-transparent text-ink shadow-[inset_0_0_0_1px_var(--line-strong)] hover:bg-core",
};

function TrailingIcon({ children, variant }: { children: ReactNode; variant: keyof typeof variants }) {
  return (
    <span
      className={
        "flex h-8 w-8 items-center justify-center rounded-full transition-transform duration-500 ease-spring " +
        "group-hover:translate-x-0.5 group-hover:-translate-y-px " +
        (variant === "primary" ? "bg-white/15" : "bg-accent-wash text-accent")
      }
    >
      {children}
    </span>
  );
}

export function LinkButton({
  href,
  children,
  variant = "primary",
  external = false,
}: {
  href: string;
  children: ReactNode;
  variant?: keyof typeof variants;
  external?: boolean;
}) {
  return (
    <a
      href={href}
      className={`${pill} ${variants[variant]}`}
      {...(external ? { target: "_blank", rel: "noreferrer" } : {})}
    >
      {children}
      <TrailingIcon variant={variant}>
        <ArrowUpRight size={15} weight="light" aria-hidden />
      </TrailingIcon>
    </a>
  );
}

export function ActionButton({
  onClick,
  children,
  icon,
  disabled,
  variant = "primary",
}: {
  onClick: () => void;
  children: ReactNode;
  icon: ReactNode;
  disabled?: boolean;
  variant?: keyof typeof variants;
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} className={`${pill} ${variants[variant]}`}>
      {children}
      <TrailingIcon variant={variant}>{icon}</TrailingIcon>
    </button>
  );
}

const outcomeColor: Record<Outcome, string> = {
  recovered: "text-ok",
  blocked: "text-stop",
  escalated: "text-person",
  needs_review: "text-hold",
  no_action: "text-idle",
};

export function OutcomeMark({ outcome, className = "" }: { outcome: Outcome; className?: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-3 py-1 text-xs font-medium shadow-[inset_0_0_0_1px_currentColor] ${outcomeColor[outcome]} ${className}`}
    >
      {OUTCOME_LABELS[outcome]}
    </span>
  );
}

export function outcomeTextClass(outcome: Outcome): string {
  return outcomeColor[outcome];
}
