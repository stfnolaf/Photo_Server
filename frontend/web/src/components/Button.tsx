import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Button({
  children,
  tone = "default",
  compact = false,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  children: ReactNode;
  tone?: "default" | "primary" | "danger" | "ghost";
  compact?: boolean;
}) {
  return (
    <button
      className={`button button--${tone}${compact ? " button--compact" : ""} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
