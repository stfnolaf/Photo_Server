import { createContext, type ReactNode, useCallback, useContext, useMemo, useRef, useState } from "react";

type ToastTone = "default" | "error";
interface ToastValue {
  show: (message: string, tone?: ToastTone) => void;
}
const ToastContext = createContext<ToastValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<{ message: string; tone: ToastTone } | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const show = useCallback((message: string, tone: ToastTone = "default") => {
    setToast({ message, tone });
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setToast(null), 4500);
  }, []);
  const value = useMemo(() => ({ show }), [show]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      {toast && (
        <div className={`toast toast--${toast.tone}`} role="status">
          {toast.message}
        </div>
      )}
    </ToastContext.Provider>
  );
}

export function useToast() {
  const value = useContext(ToastContext);
  if (!value) throw new Error("Toast provider is unavailable");
  return value;
}
