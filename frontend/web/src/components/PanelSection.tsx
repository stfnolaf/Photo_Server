import { ChevronDown } from "lucide-react";
import type { ReactNode } from "react";

export function PanelSection({
  title,
  children,
  defaultOpen = true,
  actions,
}: {
  title: string;
  children: ReactNode;
  defaultOpen?: boolean;
  actions?: ReactNode;
}) {
  return (
    <details className="panel-section" open={defaultOpen}>
      <summary>
        <ChevronDown size={14} aria-hidden="true" />
        <span>{title}</span>
        {actions && <span className="panel-section__actions">{actions}</span>}
      </summary>
      <div className="panel-section__body">{children}</div>
    </details>
  );
}
